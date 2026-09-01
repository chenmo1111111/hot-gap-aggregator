import eslint from '@eslint/js';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**'] },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['web/src/**/*.{ts,tsx}'],
    languageOptions: {
      globals: {
        window: 'readonly',
        document: 'readonly',
        navigator: 'readonly',
        localStorage: 'readonly',
      },
    },
  },
);
