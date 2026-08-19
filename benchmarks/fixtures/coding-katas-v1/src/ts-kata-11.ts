export function solve(template: string, values: Record<string, string>): string {
  return template.replace(/\{([^}]+)\}/g, (_match, key) => values[key] ?? "");
}
