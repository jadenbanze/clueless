'use strict';

const js = require('@eslint/js');
const security = require('eslint-plugin-security');
const globals = require('globals');

module.exports = [
  { ignores: ['node_modules/**', 'dist/**', 'out/**'] },
  js.configs.recommended,
  security.configs.recommended,
  {
    files: ['**/*.js'],
    languageOptions: { ecmaVersion: 2023, sourceType: 'commonjs' },
    rules: {
      'no-unused-vars': ['error', { argsIgnorePattern: '^_', caughtErrors: 'none' }],
      // Object lookups keyed by validated enums (source names, modes) are the
      // norm here and every one is allowlisted first; the rule cannot see that.
      'security/detect-object-injection': 'off',
      // Paths are built from app.getPath()/__dirname, never from renderer input.
      'security/detect-non-literal-fs-filename': 'off',
      'no-restricted-syntax': ['error',
        { selector: "AssignmentExpression[left.property.name='innerHTML']", message: 'Use textContent / DOM nodes: transcript and model output are untrusted.' },
        { selector: "CallExpression[callee.name='eval']", message: 'eval is forbidden.' },
      ],
    },
  },
  {
    files: ['main.js', 'preflight.js', 'eslint.config.js', 'test/**/*.js', '*-preload.js', 'preload.js', 'build/*.js'],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    files: ['renderer.js', 'settings.js', 'onboarding.js', 'requirements.js'],
    languageOptions: { globals: { ...globals.browser } },
  },
];
