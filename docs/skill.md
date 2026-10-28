# Claude Skill

The skill in `skills/` (`SKILL.md`) defines, step by step, how a model turns
a natural-language weather question into the structured parameters
(station, metric, aggregation, date range) needed to call the
[query layer](query_layer.md).

## Supporting files

- `references/count_group.md`: rules for events
  (storms, rain spells), month/year aggregates, and climate averages such
  as "average January high". The app adds it to the system prompt only when
  the question needs it, to keep the base prompt small.
- `references/plotting.md`: rules for `/plot`
  questions: picking the grain, normals and anomalies, trend lines, daily
  climate graphs, and running totals. Loaded only on chart turns.
- `references/answer_phrasing.md`: the prompt for the second Claude call,
  which turns query results into a 1-2 sentence answer.
- `assets/obs_schema.md`, `assets/stations_schema.md`: column-level schemas
  of the `observations` and `stations` tables, the semantic context behind
  each metric and station field.
- `assets/count_group_triggers.txt`: List of trigger words that gets loaded
  into the question-answer pipeline.
- `assets/examples.json`: the worked examples, in three sets (`skill`,
  `count_group`, `plotting`). Each Markdown file has a marker line where the
  app renders its set into the system prompt (`app/pipeline.py`), so the
  examples are data that tests can check. Read the Markdown files directly
  and the examples are missing.
- `scripts/resolve_date_range.py`: deterministic date math (seasons,
  "last Friday", month lengths, leap years), kept here so the skill works on
  its own. The app runs an identical copy in `wxcore/dates.py` and exposes it
  to Claude as a tool, so the model never does calendar arithmetic itself.

## Prompt caching

The skill text is sent as a cached system-prompt block, with the date, home
city, and conversation after it, so the 2-3 resolver calls per question
re-read the skill at a tenth of the input cost.
