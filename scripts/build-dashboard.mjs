import { cpSync, mkdirSync, rmSync } from "node:fs";

rmSync("dist", { force: true, recursive: true });
mkdirSync("dist", { recursive: true });
cpSync("public", "dist", { recursive: true });
