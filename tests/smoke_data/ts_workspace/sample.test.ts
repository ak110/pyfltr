import { add } from "./sample";

const expected = 3;
const actual = add(1, 2);
if (actual !== expected) {
  throw new Error(`smoke vitest failed: ${actual} !== ${expected}`);
}
