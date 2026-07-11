# Domain Docs

This repository uses a single domain context.

## Read before implementation

- `CONTEXT.md` for the canonical project vocabulary.
- `docs/adr/` for decisions affecting the area being changed.
- `docs/TECHNICAL_GLOSSARY.md` for technical and operational terms.
- `HEALTH_ANALYTICS_ARCHITECTURE.md` for scope, roadmap, and V0.1 acceptance criteria.

Use glossary terms in issues, tests, code, and documentation. Do not replace terms with synonyms listed under `_Avoid_`.

If a proposal conflicts with an ADR, surface the conflict explicitly instead of silently overriding the decision.

## Layout

```text
/
├── CONTEXT.md
├── HEALTH_ANALYTICS_ARCHITECTURE.md
└── docs/
    ├── TECHNICAL_GLOSSARY.md
    └── adr/
```
