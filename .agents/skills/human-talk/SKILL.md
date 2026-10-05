---
name: hai-simplified-technical
description: |
  Writes, rewrites and reviews technical documentation in Simplified Technical English (ASD-STE100) or its Chinese adaptation (简化技术中文): one word per concept, short sentences, named actors, no vague words or hidden verbs, imperative steps with the condition first, warnings before the step. Use for design docs, READMEs, API docs, runbooks, migration guides, commit and PR text, code comments and error messages, or when asked for STE, controlled language or plainer docs（按 STE 写、受控中文、写得干净点、去掉翻译腔）. Not for marketing, posts, speeches or fiction. Use hai-rewrite-doc to rebuild content and hai-audit-docs to check facts.
---

# Hai Simplified Technical

For Chinese readers, see `SKILL.zh_CN.md`. The English `SKILL.md` is the execution source of truth.

## Overview

Make a technical document readable in one way only: one word for one concept, one topic in one
sentence, a named actor for each action, and a fixed set of words for requirements. The rules come
from the nine sections of ASD-STE100 Part 1. The English rules keep the STE grammar rules that
matter for software text. The Chinese rules drop the English-only grammar and add the faults of
Chinese technical writing: hidden verbs (进行, 加以), long pre-noun modifiers, missing subjects,
unclear pronouns.

Both languages share one rule numbering. `references/rules-en.md` and `references/rules-zh.md`
hold the full rules with reasons, examples and checks.

## Scope

**Technical documents only**: text that a reader uses to understand a system, do an operation or
make a technical decision. Design docs, READMEs, API references, runbooks, troubleshooting and
migration guides, commit messages, PR descriptions, code comments, error messages and UI help text
are in scope.

**Not marketing or creative text**: marketing copy, brand stories, blog and social posts, launch
announcements, speeches, fiction. That text works through rhythm, emotion and rhetoric. These
rules make it flat and defeat its purpose. Write it normally and do not apply this skill.

**Mixed documents**: release notes and announcements often mix promotion and technical content.
Apply the rules to the technical parts only (what changed, how to upgrade, what breaks).

If the user explicitly asks for these rules on non-technical text, do it, and say once that the
text will lose its persuasive tone.

## Language

- Write each part in the rules of its own language. Detect the language from the document, not
  from the conversation.
- A Chinese document with English terms uses the Chinese rules; 9.1 decides how the terms appear.
- A bilingual document applies each language's rules to its own text.
- A translation is not part of this skill. When the user asks for one, translate first, then apply
  the rules of the target language.

## Workflow

1. **Check the scope.** Confirm that the text is technical (see Scope).
2. **Find the project conventions.** Look for a glossary and a style guide (section 10 of the rule
   files says where). Project conventions win over these rules, for example a quotation style or
   a fixed translation.
3. **Write or rewrite.** Follow the summary below. Open the rule file of the language when a rule
   is unclear or when you need an example.
4. **Run the mechanical check.** `python3 <skill dir>/scripts/check.py <file>` (add `--lang en` or
   `--lang zh` to force one language). It lists places to look at, not verdicts: quoted examples,
   names next to code, and soft rules kept for a reason can stay.
5. **Deliver.** Follow `references/output-template.md`.

## Rule summary

Obey every hard rule. Obey about 80% of the soft rules, and have a reason for each one you break.

**1 Words**
- 1.1 hard: One word for one concept. Do not vary words for style.
- 1.2 hard: One meaning for each word.
- 1.3 hard: Use the glossary. Name a new concept before you use it.
- 1.4 hard: No vague words. EN: appropriate, relevant, various, significant, fairly, basically.
  ZH: 适当、有关的、一定程度上、较为、基本上、若干、显著、大幅、明显. Give a number, a name or a condition.
- 1.5 soft: Verbs that say what happens. EN: handle, process, manage, optimize. ZH: 处理、操作、支持、优化、调整.

**2 Noun phrases**
- 2.1 soft: At most two modifiers before a noun. ZH: at most two 的.
- 2.2 soft: At most three nouns together.
- 2.3 hard: Modifier markers. EN: hyphenate a compound modifier before a noun. ZH: use 的/地/得 correctly.

**3 Verbs**
- 3.1 hard: Do not hide the verb in a noun. EN: perform, carry out, conduct, make an adjustment.
  ZH: 进行、加以、予以、做出、实现了对……的.
- 3.2 soft: Active voice; name the actor. EN: passive. ZH: 被, 由……做.
- 3.3 soft: State facts in the simple tense. EN: no "is sending". ZH: no 正在, no 着 for state.
- 3.4 hard: Fixed requirement words. EN: must / must not / should / should not / can / need not
  (or the project's RFC 2119 words). ZH: 应 / 不应 / 宜 / 不宜 / 可 / 不必.
- 3.5 soft, EN only: A one-word verb when one exists ("find", not "find out").

**4 Sentences**
- 4.1 hard: One topic in each sentence.
- 4.2 hard: Name the actor. EN: no empty "It is / There is", no dangling modifiers. ZH: write the
  subject when it changes.
- 4.3 hard: Each pronoun points to one thing. EN: no bare "this". ZH: 它、这、该、其.
- 4.4 soft: Show the connection: because, so, if, but / 因为、所以、如果、但.
- 4.5 soft: A vertical list for three or more items.
- 4.6 hard, EN only: Do not leave out articles or "that" (no telegraph style).

**5 Procedures**
- 5.1 hard: Numbered list, one instruction in each step.
- 5.2 hard: Start each step with an imperative verb.
- 5.3 hard: Condition before instruction ("If X, do Y").
- 5.4 soft: Step sentences at most 20 words (EN) or 40 characters (ZH).
- 5.5 soft: Reasons before the list; each result after its step.

**6 Descriptive writing**
- 6.1 hard: Conclusion first in each paragraph.
- 6.2 soft: One topic in each paragraph, at most six sentences.
- 6.3 soft: Descriptive sentences at most 25 words (EN) or 50 characters (ZH).
- 6.4 hard: Every measured number has its conditions and date.
- 6.5 hard: Keep what is done apart from what is planned.

**7 Warnings**
- 7.1 hard: A warning comes before the step it applies to.
- 7.2 hard: The instruction first, then the risk.
- 7.3 soft: One risk in each warning.
- 7.4 hard: Two levels. **Warning / 警告:** data loss, irreversible, wrong without an error,
  production impact, publishing outside. **Caution / 注意:** slower, rework, long wait.

**8 Punctuation, numbers, length**
- 8.1 hard: EN: ASCII punctuation. ZH: full-width punctuation.
- 8.2 hard: One quotation style in each document.
- 8.3 hard: EN: a space between number and unit. ZH: a space between Chinese and Latin letters or digits.
- 8.6 hard: Ranges with "–" (or "to" / "到"), never "~".
- 8.7 hard: Dates that cannot be misread; ISO in tables and parentheses.
- 8.8 hard: Count EN words between spaces; count ZH characters, plus one for each Latin word, number or code span.

**9 Writing practices**
- 9.1 hard: One form for each term. EN: one spelling and case. ZH: the original or one fixed translation.
- 9.2 hard: Code names in backticks, exactly as in the code.
- 9.3 hard: Official product names; abbreviations defined at first use.
- 9.4 soft: No idioms, filler or marketing words. EN: seamless, leverage, simply, just, easily.
  ZH: 开箱即用、无缝、一劳永逸、赋能.
- 9.7 soft, EN only: No contractions; no e.g., i.e., etc., via.

## Do not overdo it

The rules make the reader understand one meaning. They do not make the text a telegram.

- **The 80% has limits.** Keep every hard rule. Break a soft rule when obeying it would make the
  sentence awkward or lose information; each rule gives its reason, and where the reason does not
  apply, the rule need not either.
- **The rules control wording, not content.** Keep every fact, condition, exception and doubt of
  the original. If the original misses something the reader needs (a risk, a rollback, a stale
  cache), still say so, as a note or an open question. Do not drop domain judgment to look plain.
- **Rewriting is not auditing.** Keep each claim of the author at the strength it was written;
  remove only the empty intensifier ("improved significantly" → "improved", "various other
  services" → "other services"). Do not ask the document to prove its claims inline. List the
  missing evidence for the author in the report instead (`references/output-template.md`).
- **Inline placeholders are rare.** Write "[TBD: …]" / "[待补：……]" in the document only when the
  reader cannot do the task without the fact (which flag to set, which key to check). Never put a
  guess in a placeholder, and never write a plausible number.
- **Do not touch what must stay.** Quotations, code, command output, proper names and the user's
  own words stay as they are.
- **Keep the structure.** Reorder sentences, but keep sections, headings and links unless the user
  asks for more.

## Use a different skill when

- The document's content is out of date and must be rebuilt from verified conclusions — use
  `hai-rewrite-doc`, then apply this skill to the wording if the user wants it.
- The question is whether the document is true or consistent with the code — use `hai-audit-docs`.
- Only the Markdown layout is wrong — use `readme-beautifier`.
- The question is what to call a concept across modules — use `hai-naming`; record the result in
  the glossary.
- The task is to write or shape a PRD — use `hai-prd`. This skill can polish its wording later.

## Common mistakes

- Applying the rules to marketing copy and making it flat.
- Turning every unmeasured claim into an inline "[TBD]": the document becomes a checklist of
  gaps. Report the gaps to the author; keep the document readable.
- Putting a guessed value in a placeholder, or writing a plausible number.
- Removing a caveat or a risk because the sentence got shorter without it.
- Changing a correct technical term to obey 1.4 or 9.4.
- Reporting every soft-rule hit of the script as a defect.
