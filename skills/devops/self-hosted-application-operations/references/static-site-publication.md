# Static-site and SPA publication

Absorbs the portable checks from `publish-site`; use the existing homelab deployment and ingress runbooks, not a cloud-provider ladder.

## Build and preview

1. Resolve the intended host, service, route, audience and current release. Reuse existing homelab infrastructure. External hosting, public repositories and new tunnels require an explicit user request; do not create them as defaults.
2. Build from a pinned commit in an isolated checkout. Publish the output directory (`dist/`, `build/`, exported site), not source. Confirm `index.html` and referenced assets exist; verify case-sensitive filenames and the configured base path.
3. Inspect staged files and generated output for credentials, `.env`, private documents and source maps exposing sensitive data. Client bundles cannot keep secrets; server-side secrets must remain server-side.
4. Preview through an existing LAN/VPN or authenticated preview route reachable from the user's device. Obtain sign-off when requested; do not present localhost as a cross-device URL. Teardown only preview resources owned by this task.

## Version and deploy

5. Record commit, unique release tag or release identifier, build command, artifact hashes and previous release. Stage only intended files; never use blanket `git add -A` in a shared worktree.
6. Deploy the built artifact to a versioned release directory/container using the existing service mechanism. Preserve the previous artifact and route configuration. Switch only the target service after readiness checks; do not modify unrelated services.

## Acceptance

7. Check origin and actual user-facing route independently with bounded readiness retries. A process or HTTP 200 alone is insufficient: assert expected page content and deployed revision.
8. Fetch at least one critical JS/CSS/image asset; assert correct content type and body, not the SPA fallback HTML. Check filename case, base paths and browser console/network errors.
9. For an SPA, open and refresh a real deep route; verify fallback serves the application correctly. Unknown asset/API paths must not silently return index.html. For a static multi-page site, verify an actual nested page instead of imposing SPA fallback.
10. Exercise one user flow through the real ingress. Check authentication/privacy expectations and origin/edge parity. Separate DNS/TLS/edge failures from origin failures; verify a newly changed route rather than relying on a warm cache.

## Rollback

11. Roll back by switching to the previous immutable artifact or rebuilding its pinned revision in another checkout, then rerun page/asset/deep-route checks. Never overwrite a dirty tree with `git checkout <tag> -- .` or hand-edit live output.
12. Report reachable URL, deployed commit/release, verified checks, previous release and exact rollback mechanism. Disclose any checks not exercised; a successful upload is not a successful deployment.
