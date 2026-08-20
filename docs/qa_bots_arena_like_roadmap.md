# QA Bots Arena-Like Roadmap

This document summarizes the plan for making the Academy/QA bots behave more like the Arena bot while preserving the stricter trial-group logic.

## Context

The project has three WhatsApp bots:

- `dopsy_bot`: Arena field booking bot.
- `dopsy_fs_school`: Football academy QA/trial bot.
- `dopsy_boxing`: Boxing academy QA/trial bot.

The Arena bot is the strongest architectural reference because it separates:

- LLM intent classification.
- LLM structured extraction.
- deterministic business logic.
- database-backed drafts/sessions.
- manager/payment side effects.
- RAG fallback for general questions.

The Academy bots should copy this architecture, but **not** the Arena booking logic directly.

## Key Difference: Booking vs Trial

Arena booking fields are mostly independent:

```text
date
time
field
name
players
```

So the Arena bot can accept booking fields in almost any order, merge them into a draft, and check availability once enough fields exist.

Trial signup is different:

```text
child_birth_year
experience
school_shift
```

These determine eligible groups. Eligible groups then determine valid dates and times.

Therefore, the trial bot must use a **gated flexible flow**:

```text
Accept user data in any order,
but do not offer or confirm group/date/time until prerequisites are complete.
```

## Trial Dependency Graph

```text
Trial Signup
├── Intake prerequisites
│   ├── child_name
│   ├── child_birth_year
│   ├── experience
│   └── school_shift
│
├── Eligibility filtering
│   ├── bot type: football / boxing
│   ├── child_birth_year
│   ├── experience
│   ├── school_shift
│   ├── group active status
│   └── capacity
│
├── Eligible groups
│   ├── group_type
│   ├── birth_years
│   ├── level
│   ├── max_cap / curr_cap
│   └── schedules
│
├── Date/time selection
│   ├── only from eligible groups
│   ├── user cannot randomly choose group
│   ├── early user date/time is stored as preference
│   └── bot assigns group_id internally
│
└── Confirmation
    ├── child details
    ├── selected valid slot
    ├── assigned group_id
    └── confirmed trial
```

## Preferred vs Confirmed Trial Data

Early date/time values from the user are preferences only.

Draft/preference data:

```text
child_name
child_birth_year
experience
school_shift
preferred_date
preferred_weekday
preferred_time_start
preferred_time_end
language
phone
```

Confirmed trial data:

```text
child_name
child_birth_year
experience
school_shift
trial_day
start_time
end_time
group_id
language
phone
```

Important rule:

```text
preferred_* = user wishes
trial_day/start_time/end_time/group_id = validated assigned result
```

## Completed Work

### Phase 1: Audit

Completed.

Arena vs QA comparison was performed.

Main gaps found:

- QA bots had keyword/tool routing, but no Arena-style strict intent classifier.
- QA bots had no full structured extractor.
- QA trial flow was sequential, not draft-merge based.
- Trial limits were logged but not blocking in `start_trial_flow()`.
- Group/date/time needed gated handling.

### Phase 2: Target Architecture

Completed.

Target architecture:

```text
QA Bot
├── message_handler.py trial branch
├── QA intent classifier
├── QA structured extractor
├── gated trial flow handler
├── draft/preference storage
├── eligible group filtering
├── slot selection
├── confirmation
└── RAG fallback
```

### Phase 3: Intent + Extraction + DB Preferences

Implemented.

Added:

- QA intent tool schema.
- QA extractor tool schema.
- `route_trial_message()`.
- `extract_trial_details()`.
- DB preference columns:
  - `preferred_date`
  - `preferred_weekday`
  - `preferred_time_start`
  - `preferred_time_end`

Intent enum:

```text
question_price
question_schedule
question_location
question_age
question_trial_rules
trial_new
trial_continue
trial_edit
trial_status
trial_cancel
human_help
other
```

Extractor schema:

```json
{
  "child_name": "string | null",
  "child_birth_year": "integer | null",
  "experience": "Beginner | Intermediate | Advanced | null",
  "school_shift": "morning | afternoon | null",
  "preferred_date": "YYYY-MM-DD | null",
  "preferred_weekday": "0-6 | null",
  "preferred_time_start": "HH:MM | null",
  "preferred_time_end": "HH:MM | null"
}
```

### Phase 4: Gated Trial Flow

Implemented.

Added `handlers/llm_trial_flow.py`.

Current behavior:

```text
Incoming trial message
        ↓
Classify intent
        ↓
Extract all mentioned trial data
        ↓
Create or continue draft
        ↓
Merge extracted data
        ↓
Ask only missing prerequisite
        ↓
Once prerequisites complete
        ↓
Find eligible slots
        ↓
If preferred slot matches
        └── assign group/date/time and ask confirmation
        ↓
If preferred slot missing/invalid
        └── show valid eligible slots
```

Gated session states:

```text
trial_intake
trial_select_slot
trial_confirm
```

Current no-eligible-groups behavior:

```text
1. Stop slot selection.
2. Delete active trial session.
3. Keep the draft trial row as draft.
4. Tell the user that an administrator should help.
```

Current limitation:

`experience` is collected, but group filtering cannot fully use it until groups have a level field.

## Remaining Roadmap

## Phase 5: Smart Eligibility + Fallback

Implemented.

Purpose: handle no exact group match intelligently.

No eligible group can mean:

```text
birth_year mismatch
experience mismatch
school_shift/schedule mismatch
capacity full
no active schedule
```

Planned fallback rules:

```text
1. Never relax birth year / age.
2. Never relax bot type.
3. Never offer full groups.
4. Experience can be relaxed downward.
5. School shift can be relaxed only with explicit user consent.
```

Experience fallback ladder:

```text
Advanced
├── exact: Advanced
├── fallback 1: Intermediate
└── fallback 2: Beginner

Intermediate
├── exact: Intermediate
└── fallback 1: Beginner

Beginner
└── no lower fallback
```

Do not fallback upward:

```text
Beginner -> Intermediate
Intermediate -> Advanced
```

Required DB addition:

```sql
ALTER TABLE academy_groups
ADD COLUMN level TEXT;
```

Allowed values:

```text
Beginner
Intermediate
Advanced
```

Important rollout note:

```text
Managers/data seeders must populate academy_groups.level.
Without group levels, experience-based exact/fallback matching cannot work correctly.
```

Implemented components:

```text
Phase 5A: DB + repo
├── add academy_groups.level
├── add CHECK constraint
├── include level in group queries
└── include level in Sheets group sync

Phase 5B: fallback matching
├── exact level first
├── then lower level
└── preserve age/type/capacity rules

Phase 5C: fallback consent state
└── trial_fallback_offer

Phase 5D: unresolved handling
├── admin handoff
└── session cleanup
```

Still deferred:

```text
structured rejection reason object
needs_manager state
bot pause on unresolved cases
```

Fallback offer example:

```text
RU:
Подходящей группы уровня Advanced сейчас нет.
Есть группа уровнем ниже — Intermediate, по возрасту и времени подходит.
Записать на пробное туда?

KK:
Қазір Advanced деңгейіне сәйкес топ жоқ.
Бір деңгей төмен Intermediate тобы бар, жасы мен уақыты сәйкес.
Сынақ сабағына сол топқа жазайын ба?
```

## Phase 6: Trial Service Layer

Purpose: make QA mutations reliable like Arena's `booking_service.py`.

Create:

```text
integrations/trial_service.py
```

Responsibilities:

```text
create_draft
update_intake
assign_slot
confirm_trial
cancel_trial
enforce_trial_limits
sync_trial_sheets
```

Use a typed result envelope:

```json
{
  "ok": true,
  "code": "OK",
  "data": {},
  "message": ""
}
```

Comparison:

```text
Arena:
booking_service.py owns booking state changes

QA target:
trial_service.py owns trial state changes
```

## Phase 7: Status / Edit / Cancel Upgrade

Purpose: make trial self-service closer to Arena.

Add or improve:

```text
trial_status
trial_cancel
trial_edit
```

Current weakness:

```text
cancel_all_trials() deletes all draft/confirmed trials for the phone.
```

Target behavior:

```text
trial_status
└── show user's active/upcoming trial

trial_cancel
├── select relevant active trial
├── avoid deleting all blindly
└── return exact status

trial_edit
├── edit child_name
├── edit birth_year
├── edit experience
├── edit school_shift
├── re-run eligibility if needed
└── re-confirm if group/time changes
```

## Phase 8: RAG / QA Knowledge Upgrade

Purpose: improve factual QA answers.

Target:

```text
separate knowledge context by bot
├── football school docs
└── boxing academy docs

availability context
├── generic schedule for factual schedule questions
└── no slot promises without intake prerequisites

prompt policy
├── no invented prices
├── no invented groups
├── no invented free slots
├── admin handoff when uncertain
└── same language as user
```

Comparison:

```text
Arena:
documents/*.md + availability context + system prompt

QA target:
academy-specific docs + gated availability context + stricter answer policy
```

## Phase 9: Manager Handoff / Needs Manager

Purpose: handle unresolved cases operationally.

Possible triggers:

```text
no eligible group
user rejects fallback
ambiguous edit/cancel
capacity full
user asks for admin
```

Target:

```text
add needs_manager state or flag
optionally pause bot for that phone
expose needs_manager in manager UI/API
avoid endless bot loops
```

Potential states:

```text
draft
confirmed
cancelled
needs_manager
```

## Phase 10: Test Coverage + Hardening

Purpose: make the new QA flow safe.

Required tests:

```text
intent classification
structured extraction
draft continuation
missing prerequisite prompts
preferred_* storage
exact eligibility
fallback eligibility
fallback consent
slot selection
confirmation
cancel/edit/status
no eligible group handling
manager handoff
regression tests for Arena bot
```

Note: the current test harness skips all tests when `POSTGRES_DSN` is unset because of the global DB fixture in `tests/conftest.py`.

## Phase 11: Cleanup / Deprecate Old Trial Flow

Purpose: reduce duplicate logic after the gated flow is stable.

Cleanup targets:

```text
remove or shrink old sequential trial flow
keep reusable prompt helpers if useful
consolidate trial messages
remove duplicated confirmation logic
remove stale child_age references
document final QA flow
```

## Recommended Order

```text
5. Smart Eligibility + Fallback
6. Trial Service Layer
7. Status / Edit / Cancel Upgrade
8. RAG / QA Knowledge Upgrade
9. Manager Handoff / Needs Manager
10. Test Coverage + Hardening
11. Cleanup
```

Recommended next task: Phase 5, because it directly affects the correctness of group selection.
