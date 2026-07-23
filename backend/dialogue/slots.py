"""
Static definition of every slot-collecting stage: what to ask, and how
to interpret the answer. Dynamic stages (plan_choice, plan_action, closing)
are handled separately in state_machine.py because their options depend
on session data, not a fixed list.
"""

STAGES = {
    "full_name": {
        "prompt": "Hi! I'll get you a quote in just a couple minutes. What's your full name?",
        "kind": "text",
    },
    "phone": {
        "prompt": "What's your phone number?",
        "kind": "phone",
    },
    "email": {
        "prompt": "And what's your email address?",
        "kind": "email",
    },
    "street": {
        "prompt": "What's your street address?",
        "kind": "text",
    },
    "city": {
        "prompt": "Which city is that in?",
        "kind": "text",
    },
    "zip": {
        "prompt": "And your zip code?",
        "kind": "zip",
    },
    "category": {
        "prompt": "Are you looking for Heating, Cooling with Electric Heat, or Cooling with a Heat Pump?",
        "kind": "choice",
        "options": [
            {"value": "heating", "label": "Heating"},
            {"value": "cooling_electric_heat", "label": "Cooling with Electric Heat"},
            {"value": "cooling_heat_pump", "label": "Cooling with Heat Pump"},
        ],
    },
    "tonnage": {
        "prompt": "What size is your current system, in tons - 2, 2.5, 3, 3.5, or 4?",
        "display_prompt": "What size is your current system?",
        "kind": "choice",
        "options": [
            {"value": "2_ton", "label": "2 Ton"},
            {"value": "2.5_ton", "label": "2.5 Ton"},
            {"value": "3_ton", "label": "3 Tons"},
            {"value": "3.5_ton", "label": "3.5 Tons"},
            {"value": "4_ton", "label": "4 Ton"},
        ],
    },
    "location": {
        "prompt": "Where's your indoor air handler - attic horizontal, closet vertical, or garage?",
        "display_prompt": "Where's your indoor air handler?",
        "kind": "choice",
        "options": [
            {"value": "attic_horizontal", "label": "Attic Horizontal"},
            {"value": "closet_vertical", "label": "Closet (Vertical)"},
            {"value": "garage_vertical", "label": "Garage"},
        ],
    },
    "call_timing": {
        "prompt": "Would you like us to call you right now, or schedule a specific day and time?",
        "kind": "choice",
        "options": [
            {"value": "immediate", "label": "Call Me Right Now"},
            {"value": "schedule", "label": "Schedule A Specific Time"},
        ],
    },
}

# The linear backbone. category/location/plan_action/call_timing override
# this with branching logic in state_machine._advance(); everything else
# just walks to the next item in this list.
ORDER = [
    "full_name", "phone", "email", "street", "city", "zip",
    "category", "tonnage", "location", "plan_choice", "review_summary", "plan_action",
    "call_timing", "schedule_appointment",
]