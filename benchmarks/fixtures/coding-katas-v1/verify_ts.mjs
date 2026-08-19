import { readFile } from "node:fs/promises";

const taskId = process.argv[2];
const root = new URL("./", import.meta.url);
const cases = JSON.parse(await readFile(new URL("cases.json", root), "utf8"))[taskId];
const subject = await import(new URL(`src/${taskId}.ts`, root).href);

for (const [index, item] of cases.entries()) {
  const actual = subject.solve(...item.args);
  if (JSON.stringify(actual) !== JSON.stringify(item.expected)) {
    console.error(`case ${index}: expected ${JSON.stringify(item.expected)}, got ${JSON.stringify(actual)}`);
    process.exit(1);
  }
}
