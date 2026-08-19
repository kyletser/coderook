const taskId = process.argv[2];
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const subject = await import(new URL(`src/${taskId}.ts`, import.meta.url).href);
const decoded = subject.solve(chunks.join(""));
process.stdout.write(JSON.stringify(decoded));
