import { execFileSync } from "node:child_process";

/** Making an account an administrator from outside the API, as a deployment must (P10).
 *
 * Shelling out to `poe grant-admin` is the point rather than a workaround: there is
 * deliberately no API for this, because the API's own answer to "who may grant admin" is "an
 * administrator", and a deployment starts with none. Not a spec file, so it is imported rather
 * than run.
 */

const REPO = new URL("../..", import.meta.url).pathname;

/** The database the e2e API is on — derived exactly as `scripts/e2e-backend.sh` derives it, so
 * there is no second DSN to keep in step with it. */
function e2eDatabaseUrl(): string {
  return execFileSync(
    "uv",
    ["run", "python", "-m", "tests.testdb", "--suffix", "_e2e", "--print-url"],
    {
      cwd: REPO,
      encoding: "utf8",
    },
  ).trim();
}

export function grantAdmin(email: string): void {
  execFileSync("uv", ["run", "poe", "grant-admin", email], {
    cwd: REPO,
    encoding: "utf8",
    env: { ...process.env, GURU_DATABASE_URL: e2eDatabaseUrl() },
  });
}
