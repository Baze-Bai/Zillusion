# Goal

Collect the front page of Hacker News with one level of comment data.

## Required fields

- For each front-page story (top 30):
  - `title`, `url`, `score`, `author`, `comment_count`, `story_id`
- For each story, the first ~10 top-level comments:
  - `author`, `text`, `posted_relative`

## Scope

- Single front page; no deeper pagination.
- Top-level comments only.

## Constraints

- No login required.
- Output deterministic given crawl moment.
