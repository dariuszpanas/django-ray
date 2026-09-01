export const titleRules = {
  "header-max-length": [2, "always", 72],
  "subject-empty": [2, "never"],
  "subject-min-length": [2, "always", 10],
};

const validationTrailerRule = (parsed) => {
  const footerLines = parsed.footer?.split(/\r?\n/u) ?? [];
  const hasValidation = footerLines.some((line) => /^Validation:\s+\S/u.test(line));

  return [hasValidation, "footer must contain a non-empty `Validation:` trailer"];
};

export default {
  extends: ["@commitlint/config-conventional"],
  defaultIgnores: false,
  plugins: [
    {
      rules: {
        "validation-trailer": validationTrailerRule,
      },
    },
  ],
  rules: {
    ...titleRules,
    "body-empty": [2, "never"],
    "body-leading-blank": [2, "always"],
    "body-max-line-length": [2, "always", 72],
    "body-min-length": [2, "always", 100],
    "footer-leading-blank": [2, "always"],
    "footer-max-line-length": [2, "always", 100],
    "validation-trailer": [2, "always"],
  },
};
