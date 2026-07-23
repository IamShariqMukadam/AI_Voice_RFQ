// AudioWorkletProcessors for mic capture and playback.
//
// These replace the two ScriptProcessorNode instances that used to live in
// realtime-widget.js (ensureMic's capture node, ensurePlaybackNode's ring-
// buffer node). ScriptProcessorNode runs its callback on the main thread,
// so any main-thread contention (GC pause, DOM work, a busy tab) can stall
// audio capture/playback and produce exactly the choppy-audio symptom this
// migration is meant to fix. AudioWorkletProcessor.process() runs on a
// dedicated realtime audio thread instead, isolated from main-thread work.
//
// The algorithms below are unchanged from the ScriptProcessorNode versions -
// only the thread they run on has moved.

class MicCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // Matches the old createScriptProcessor(4096, 1, 1) buffer size, so
    // downstream code (VAD gate, resampler) keeps seeing the same chunk
    // shape it always has.
    this._bufSize = 4096;
    this._buf = new Float32Array(this._bufSize);
    this._writeIdx = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      for (let i = 0; i < channel.length; i++) {
        this._buf[this._writeIdx++] = channel[i];
        if (this._writeIdx === this._bufSize) {
          // Transferable postMessage - zero-copy handoff to the main
          // thread, same as the old onaudioprocess event.inputBuffer data.
          this.port.postMessage(this._buf, [this._buf.buffer]);
          this._buf = new Float32Array(this._bufSize);
          this._writeIdx = 0;
        }
      }
    }
    return true; // keep the processor alive for the life of the call
  }
}

class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._queue = [];      // Float32Array chunks waiting to be played, in order
    this._readOffset = 0;
    this._wasPlaying = false;
    this._queuedSamples = 0;
    // postMessage delivery from the main thread (WS message -> enqueue) is
    // an async hop that the old same-thread array push didn't have, so a
    // brief main-thread delay can leave the queue momentarily empty right
    // as a response starts. Hold output silent until ~50ms is buffered (or
    // ~200ms elapses, so a short reply doesn't wait forever) to absorb that
    // jitter instead of audibly stuttering.
    this._priming = true;
    this._primeQuantums = 0;
    this._PRIME_SAMPLES = 1200;   // ~50ms @ 24kHz
    this._PRIME_MAX_QUANTUMS = 40; // ~200ms @ 128-sample quanta
    // BUG FIX (chopped voice / mic ducking): the queue running dry used
    // to be treated as "response over" unconditionally. But audio.delta
    // packets arrive in bursts (worse when the model spends reasoning
    // tokens between words), so the queue empties MANY times per
    // response under totally normal conditions - each one used to fire
    // playbackEnded early, unmuting the mic and re-priming mid-sentence.
    // _expectingMore tracks whether the backend has told us this
    // response is actually finished (see "endOfResponse" below, sent
    // from realtime_session.py's response.done handler); until then, a
    // dry queue is just an underrun - output silence and keep waiting,
    // don't unmute the mic or restart the prime buffer.
    this._expectingMore = false;
    this.port.onmessage = (event) => {
      const msg = event.data;
      if (msg.type === "enqueue") {
        this._queue.push(msg.chunk);
        this._queuedSamples += msg.chunk.length;
        this._expectingMore = true;
      } else if (msg.type === "endOfResponse") {
        this._expectingMore = false;
      } else if (msg.type === "clear") {
        this._queue = [];
        this._readOffset = 0;
        this._queuedSamples = 0;
        this._wasPlaying = false;
        this._expectingMore = false;
        this._priming = true;
        this._primeQuantums = 0;
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (this._priming) {
      this._primeQuantums++;
      const primed = this._queuedSamples >= this._PRIME_SAMPLES
        || this._primeQuantums >= this._PRIME_MAX_QUANTUMS
        || (!this._queue.length && this._primeQuantums > 1); // nothing arrived at all - don't hang
      if (!primed) {
        out.fill(0);
        return true;
      }
      this._priming = false;
    }
    let filled = 0;
    while (filled < out.length && this._queue.length) {
      const chunk = this._queue[0];
      const avail = chunk.length - this._readOffset;
      const need = out.length - filled;
      const take = Math.min(avail, need);
      out.set(chunk.subarray(this._readOffset, this._readOffset + take), filled);
      filled += take;
      this._readOffset += take;
      this._queuedSamples -= take;
      if (this._readOffset >= chunk.length) {
        this._queue.shift();
        this._readOffset = 0;
      }
    }
    if (filled < out.length) {
      out.fill(0, filled); // buffer underrun - true silence, not a click
    }
    if (this._queue.length) {
      this._wasPlaying = true;
    } else if (this._wasPlaying && !this._expectingMore) {
      // Nothing left queued, nothing left to drain, AND the backend has
      // confirmed no more audio.delta packets are coming for this
      // response - only now is the reply actually over.
      this._wasPlaying = false;
      this._priming = true;    // re-arm the pre-buffer for the next reply
      this._primeQuantums = 0;
      this.port.postMessage({ type: "playbackEnded" });
    }
    // else: queue is momentarily empty but more audio is still expected -
    // this is ordinary network/generation jitter, not the end of the
    // turn. Output silence for this one render quantum (already done
    // above) and keep _wasPlaying/priming/mic state exactly as-is so the
    // next chunk resumes instantly with no re-prime delay and no mic
    // unmute.
    return true;
  }
}

registerProcessor("mic-capture-processor", MicCaptureProcessor);
registerProcessor("playback-processor", PlaybackProcessor);