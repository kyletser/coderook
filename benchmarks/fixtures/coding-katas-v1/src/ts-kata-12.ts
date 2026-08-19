export function solve(values: number[], count: number): number[] {
  return values.sort((left, right) => left - right).slice(0, count);
}
