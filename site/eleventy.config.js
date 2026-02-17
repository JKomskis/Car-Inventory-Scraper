import path from "node:path";
import { fileURLToPath } from "node:url";
import EleventyVitePlugin from "@11ty/eleventy-plugin-vite";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default function (eleventyConfig) {
  // Watch the inventory data directory for changes during dev
  eleventyConfig.addWatchTarget(
    path.resolve(__dirname, "../inventory/")
  );

  // Pass through static assets (Vite handles JS bundling)
  eleventyConfig.addPassthroughCopy("src/styles");

  // Vite plugin for JS bundling with tree-shaking
  eleventyConfig.addPlugin(EleventyVitePlugin, {
    viteOptions: {
      resolve: {
        alias: {
          "/scripts": path.resolve(__dirname, "src/scripts"),
        },
      },
    },
  });

  // Nunjucks filter: format a number as $X,XXX
  eleventyConfig.addFilter("dollar", (value) => {
    if (value == null || value === "") return "";
    return `$${Number(value).toLocaleString("en-US")}`;
  });

  // Nunjucks filter: format adjustment with sign and color class
  eleventyConfig.addFilter("adjustment", (value) => {
    if (value == null || value === 0) return "";
    if (value < 0) return `-$${Math.abs(value).toLocaleString("en-US")}`;
    return `+$${value.toLocaleString("en-US")}`;
  });

  // Nunjucks filter: CSS class for adjustment values
  eleventyConfig.addFilter("adjClass", (value) => {
    if (value == null || value === 0) return "";
    return value < 0 ? "adj-neg" : "adj-pos";
  });

  return {
    dir: {
      input: "src",
      output: "dist",
      includes: "_includes",
      data: "_data",
    },
    templateFormats: ["njk", "html", "md"],
    htmlTemplateEngine: "njk",
  };
}
