# Blind pair judge (one pack, fresh session)

Fill the three `<…>` slots, hand ONE pack to ONE fresh judge session (pack 1 and pack 2 to separate sessions;
never both to the same context). Prefer a judge from a different model family than the one that wrote the change.

---

Pack (private customer content: read it locally, never copy it anywhere else): `<abs path to pack-N.md>`

Context: `<one paragraph: the product, who writes in (customer, admin), what a good answer does>`.
Each pair shows one real turn and two outputs X and Y from two configurations, in random order, produced
minutes apart on live data. Treat X and Y symmetrically: position and length say nothing about quality.

Judge each pair on, in this order: (1) correct and grounded (no invented screens, numbers, settings, policy);
(2) answers everything asked, or asks one precise question when the request is genuinely ambiguous (a question
is wrong when the answer was obtainable); (3) right action or none (an ACTION CARD is shown to the user to
confirm and runs only after their click: judge it as part of the answer); (4) no internal table/file/tool names;
(5) `<project terms, tone, language>`; (6) concise. Lower criteria only break ties on higher ones.

`same` is the expected verdict when neither output is meaningfully better for the person who asked: different
wording, order or length is not a difference. `clear` only when one output has a fault the asker would notice
(wrong fact, missed question, wrong/missing action, ungrounded claim, leak) and the other does not.
A fault both outputs share is not a reason to prefer either.

Write one JSON line per pair to `<abs path to cmp dir>/judge-<your judge name>-<pack number>.jsonl` (chmod 600):
`{"pair": "P001", "better": "X"|"Y"|"same", "margin": "none"|"slight"|"clear", "x_faults": [...],
"y_faults": [...], "why": "one sentence, no customer names"}`. Fault tags: factual_error, missed_question,
unneeded_question, wrong_action, missing_action, ungrounded, internals_leak, wrong_terms, verbose, other.
Then reply with only the line count. No customer content in your reply.
