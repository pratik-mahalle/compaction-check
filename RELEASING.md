# Releasing a developer alpha

Distribution is through GitHub prereleases. The tag workflow validates the exact tagged source, runs the offline suite on Linux and macOS, checks formatting and packaging, then uploads the wheel, source archive, and SHA-256 checksums.

1. Set an alpha version such as `0.2.0a1` in `pyproject.toml` and `src/compaction_check/__init__.py`. The CLI reads the package version.
2. Update `CHANGELOG.md`, the README installation URL, and `docs/releases/v<VERSION>.md`. State the evaluated scope and known limits.
3. Run the checks in `CONTRIBUTING.md`. Inspect the staged files for credentials, private traces, local paths, and unintended files.
4. Commit and push to `main`. Wait for the `Tests` workflow to pass.
5. Create an annotated tag on that tested commit and push only that tag:

   ```sh
   git tag -a v0.2.0a1 -m 'compaction-check 0.2.0a1 developer alpha'
   git push origin v0.2.0a1
   ```

6. Wait for `Release alpha` to finish. Verify the public release is marked as a prerelease, all three assets are present, and installation from the published wheel works.

The publication workflow rejects non-alpha versions, mismatched tags, missing notes, malformed distributions, and existing releases. It uses no Jev credentials and does not run live model tests. Failed checks prevent publication. A failed release with an existing draft must be inspected before retrying.

Never silently replace a published artifact. Fix defects in a new alpha version. A future stable or PyPI release needs an explicit change to this process.
