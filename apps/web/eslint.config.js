import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist', 'coverage'] },
  js.configs.recommended,
  // Type-aware linting: these rules read the type graph, so they catch what a
  // purely syntactic linter cannot (floating promises, unsafe `any` flow).
  tseslint.configs.strictTypeChecked,
  tseslint.configs.stylisticTypeChecked,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
  // `configs.flat` — the top-level presets are still eslintrc format in v7.
  reactHooks.configs.flat['recommended-latest'],
  reactRefresh.configs.vite,
  // Config files are plain JS and have no project to type-check against.
  { files: ['**/*.js'], extends: [tseslint.configs.disableTypeChecked] },
)
