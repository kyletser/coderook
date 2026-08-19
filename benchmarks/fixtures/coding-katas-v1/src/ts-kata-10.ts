export function solve(values: string[]): Record<string, number> {
  return Object.fromEntries(values.map((value) => [value, 1]));
}
