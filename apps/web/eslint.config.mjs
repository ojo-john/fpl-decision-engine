import nextConfig from "eslint-config-next";

const eslintConfig = [
  ...nextConfig,
  {
    ignores: ["lib/api-types.ts"],
  },
];

export default eslintConfig;
