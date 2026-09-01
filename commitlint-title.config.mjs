import { titleRules } from "./commitlint.config.mjs";

export default {
  extends: ["@commitlint/config-conventional"],
  defaultIgnores: false,
  rules: titleRules,
};
