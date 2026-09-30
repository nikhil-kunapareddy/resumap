# Security

## How Resumap is meant to run

Resumap is a single-user tool for your own machine. It has no accounts and no
authentication: it binds to `127.0.0.1` and trusts whoever can reach it. Do not
expose it to a network (for example by setting `RESUMAP_HOST=0.0.0.0`) unless
everyone on that network may read your resumes and spend your model budget.

What it keeps, and where:

* your resumes, job descriptions and exports, in `data/` beside the code;
* model prompts, which contain resume text, in `data/logs/prompts/`
  (size-capped; turn off with `RESUMAP_LOG_PROMPTS=0`);
* an API key, if you use the `api` backend, in `.env`.

`data/` and `.env` are git-ignored. The CLI backends are started with any API
key from `.env` removed from their environment, so a CLI cannot bill it by
accident.

## Reporting a vulnerability

Please report security problems privately, through GitHub's
"Report a vulnerability" button on the repository's Security tab, rather than
in a public issue. Include what you found and how to reproduce it.
