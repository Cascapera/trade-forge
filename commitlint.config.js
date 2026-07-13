/** Conventional Commits, verified — not merely promised (AGENTS.md §4). */
export default {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'scope-enum': [
      2,
      'always',
      ['engine', 'schema', 'api', 'web', 'collector', 'executor', 'ci', 'docs', 'repo', 'deps'],
    ],
    'body-max-line-length': [1, 'always', 100],
  },
}
