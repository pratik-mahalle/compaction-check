# Data formats

The JSON Schema files describe the serialized shape. Python's loaders also enforce byte limits, unique message IDs, chronology, exact source spans, lifecycle consistency, and content hashes. Run `compaction-check trace PATH` to validate those checks without an API call.

- `messages-v1.schema.json`: the complete ordered message array given to the agent, including separately retained host instructions. `sequence` describes order within this snapshot.
- `registry-v2.schema.json`: the current or historical registry snapshot. Source messages retain their original session sequence; `span_start` and `span_end` are Python character offsets, with the end excluded. `task_id: null` on a record means session scope. A snapshot's `task_id` selects which task-scoped rules apply.
- `trace-v1.schema.json`: one compaction boundary, its registry, optional manual retention labels, and before/after hashes. `origin` is explicitly `synthetic` or `observed`. Hashes detect accidental changes; they are not signatures or proof of trusted provenance.

Registry updates come from the host's trusted application events. Setting `source_type` to `instruction` is a host assertion, not an automatic validation of arbitrary text. Never let a tool result or document assign itself a role. The registry rejects sources marked as references, summaries, or tool results.

`Trace.from_dict()` and `load_trace()` import traces for evaluation. They do not mutate a live registry. `snapshot_from_dict()` validates an exported snapshot; it does not establish that the source is trusted. The SQLite event log remains the live registry.

The version 1 flat constraints format is still supported by `check`. Version 2 exports can also be read by that command; it uses the latest version of each rule and filters by the exported task scope. An export with no applicable active rules cannot pass a `check`. The boundary API handles an empty active set separately and explicitly makes no retention claim.

Split evaluation corpora by original session/task family before tuning. The checked-in trace variants and continuation experiments are all development fixtures. No observed or held-out production corpus has been collected.
