{% set s = scores %}
{% set d = scores.defects %}
# Vouch report: {{ meta.suite }}

Run `{{ meta.run_id }}` · started {{ meta.started_at or "unknown" }} · target `{{ meta.target }}`  
App model: {{ meta.app_model }}{% if meta.get("app_model_source") == "target" %} (reported by the app){% elif meta.get("target_type") == "http" %} (as configured; the HTTP app picks its own model){% endif %} · Judge: {{ meta.judge_model if judged else "off (rule checks only)" }}{% if judged %}, temperature {{ meta.get("judge_temperature", suite.config.judge_temperature) }}{% endif %}{{ "\n" }}
## Summary

| Metric | Value |
|---|---|
| Overall pass rate (pass^1, mean over cases) | **{{ s.pass_rate | pct }}** |
| pass^{{ s.k }} (all {{ s.k }} trials of a case pass) | **{{ s.pass_k | pct }}** |
| Trials passed | {{ s.trials_passed }} / {{ s.trials }} across {{ s.graded | length }} graded cases |
{% if d.mode == "seeded" %}
| Seeded-defect detection | **{{ d.detection_rate | pct }}** ({{ d.detected | length }} / {{ d.defect_cases | length }} cases targeting an active defect caught) |
{% elif d.mode == "clean" %}
| Seeded defects | No seeded defects active (clean run) |
{% else %}
| Seeded defects | Not reported by the target; rebuild with `vouch report <run_dir> --defects ...` to set them |
{% endif %}
{% if d.control_cases %}
| False positives ({{ d.control_label }}) | **{{ d.false_positive_rate | pct }}** ({{ d.false_positives | length }} / {{ d.control_cases | length }} flagged) |
{% endif %}
{% if judged %}
| Judgments (requirement × trial) | {{ s.judgments["pass"] }} pass · {{ s.judgments["fail"] }} fail · {{ s.judgments["unknown"] }} unknown |
{% endif %}
| Needs human review | {{ unknown_count }} unknown verdict(s) in {{ unknowns | length }} trial(s) |

A trial passes only if the app answered, every rule check passed and every judged requirement passed. Errors and `unknown` verdicts count as not passing. pass^k is the chance that *all* k fresh runs of a case pass, estimated as C(c,k)/C(n,k) from c passes in n trials.
{% if d.defect_cases or d.control_cases %}
A case is "caught" or "flagged" if at least one of its trials did not pass, including trials waiting for human review.
{% endif %}

> **Judge calibration:** not measured for this run. Until the judge is checked against human labels, treat its verdicts as unverified.


## Seeded defects

{% if d.mode == "seeded" %}
Active in this run: {{ d.active | join(", ") }} (source: {{ meta.get("defects_source", "unknown") }})

| Defect | Cases | Caught | Missed |
|---|---|---|---|
{% for x in d.per_defect %}
| `{{ x.name }}` | {{ x.cases | length }} | {{ x.caught | length }} | {{ x.missed | join(", ") | md or "–" }} |
{% endfor %}
{% elif d.mode == "clean" %}
No seeded defects active (clean run; source: {{ meta.get("defects_source", "unknown") }}). Every case should pass, so any flagged case counts as a false positive.
{% else %}
Active seeded defects were not reported by the target, so detection isn't measured. False positives are counted on cases tagged `{{ suite.config.clean_tag }}`.
{% endif %}
{% if d.false_positives %}

False positives ({{ d.control_label }}): {{ d.false_positives | join(", ") }}
{% endif %}

## Cases

| Case | Tags | Trials | Passed | Errors | pass^1 | pass^n | Rules ok | Judgments p / f / u |
|---|---|---|---|---|---|---|---|---|
{% for c in s.cases %}
{% if c.graded %}
| `{{ c.case_id }}` | {{ c.tags | join(", ") | md }} | {{ c.n }} | {{ c.passed }} | {{ c.errors }} | {{ c.pass_rate | pct }} | {{ c.pass_n | pct }} | {{ c.rules_ok if c.rules_ok is not none else "–" }} | {% set j = c.judgments() %}{{ j["pass"] }} / {{ j["fail"] }} / {{ j["unknown"] }} |
{% else %}
| `{{ c.case_id }}` | {{ c.tags | join(", ") | md }} | {{ c.n }} | – | {{ c.errors }} | not graded | | | |
{% endif %}
{% endfor %}

pass^n uses k = that case's own trial count n, so it is 100% only if every trial passed.

## By requirement

| Req | Severity | Requirement | Cases | Pass rate | pass^{{ s.k }} | Judgments p / f / u |
|---|---|---|---|---|---|---|
{% for r in s.requirements %}
| {{ r.name }} | {{ requirements[r.name].severity }} | {{ r.description | md }} | {{ r.cases }} | {{ r.pass_rate | pct if r.trials else "–" }} | {{ r.pass_k | pct if r.cases else "–" }} | {{ r.judgments["pass"] }} / {{ r.judgments["fail"] }} / {{ r.judgments["unknown"] }} |
{% endfor %}

## By severity

| Severity | Requirements | Cases | Pass rate | pass^{{ s.k }} | Judgments p / f / u |
|---|---|---|---|---|---|
{% for r in s.severities %}
| {{ r.name }} | {{ r.description }} | {{ r.cases }} | {{ r.pass_rate | pct if r.trials else "–" }} | {{ r.pass_k | pct if r.cases else "–" }} | {{ r.judgments["pass"] }} / {{ r.judgments["fail"] }} / {{ r.judgments["unknown"] }} |
{% endfor %}

Requirement and severity pass rates count whole trials of the linked cases.

## Failures ({{ failures | length }} trials)
{% if not failures %}

No failing trials.
{% endif %}
{% for f in failures %}

### `{{ f.case.id }}` · trial {{ f.grade.output.trial_idx }}{% if f.case.tags %} · {{ f.case.tags | join(", ") }}{% endif %}

{% if f.grade.output.error %}
- **Error:** {{ f.grade.output.error }}
{% endif %}
{% for c in f.check_failures %}
- **Rule** `{{ c.to_dict().label }}`: {{ c.detail }}
{% endfor %}
{% for j, req in f.judgments %}
- **Judge {{ j.requirement_id }}**{% if req %} ({{ req.severity }}){% endif %}: fail. {{ j.reason }}
  > {{ j.evidence | md }}
{% endfor %}

<details><summary>Input and output</summary>

**Input:**

~~~text
{{ f.input_text }}
~~~

**Output:**

~~~text
{{ f.grade.output.output or "" }}
~~~

</details>
{% endfor %}

## Needs human review: unknown verdicts ({{ unknown_count }})
{% if not unknowns %}

None.
{% endif %}
{% for u in unknowns %}

### `{{ u.case.id }}` · trial {{ u.grade.output.trial_idx }}

{% for j, req in u.judgments %}
- **{{ j.requirement_id }}**{% if req %}: {{ req.text }}{% endif %}  
  Reason: {{ j.reason }}{% if j.raw_verdict == "fail" %} (the judge said *fail*, but its quote wasn't found in the output){% endif %}
{% if j.evidence %}
  > {{ j.evidence | md }}
{% endif %}
{% endfor %}

**Input:**

~~~text
{{ u.input_text }}
~~~

**Output:**

~~~text
{{ u.grade.output.output or "" }}
~~~
{% endfor %}

---
Generated by Vouch {{ version }} at {{ generated_at }}. Rebuild with `vouch report <run_dir>`.
