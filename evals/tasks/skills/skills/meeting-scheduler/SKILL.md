---
name: meeting-scheduler
description: How to schedule meetings for the user. Use it whenever a task involves finding a time, booking a meeting, or answering a meeting request.
---

# Scheduling meetings

1. Read the request (the email, or the task) and note who, how long (30 minutes if not said) and which days.
2. List the calendar for those days (`calendar__list_events`). A slot is free only if it overlaps nothing.
3. Pick the earliest free slot inside the user's meeting hours (see `reference.md` in this skill's folder).
   Never book outside them, even if asked for a specific time; propose the nearest allowed slot instead.
4. Create the event with the title `<Person> / <topic>` (for example `Ravi / Q3 numbers`) and put the
   meeting link from `reference.md` in the notes.
5. Reply to the person with the day, date and time, and the meeting link.
