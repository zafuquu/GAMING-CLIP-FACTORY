# Dependency lock maintenance

The repository checks in four standard Python locks and four optional speech-recognition locks. Each lock is generated on its target OS/Python combination because wheel availability and hashes can vary by platform. The frontend uses `frontend/package-lock.json`.

## Refresh procedure

1. Commit the manifest edits to a working branch: `backend/requirements.txt`, `backend/requirements-ml.txt`, and/or `frontend/package.json`.
2. In GitHub Actions, run **Refresh dependency locks** on that branch. It uses pinned `pip-tools==7.6.2` and generates one standard lock plus one ML lock for Windows/Linux and Python 3.11/3.12. It also generates the frontend npm lock.
3. Download all five workflow artifacts: `dependency-locks-linux-py311`, `dependency-locks-linux-py312`, `dependency-locks-win-py311`, `dependency-locks-win-py312`, and `dependency-locks-frontend`. Extract each artifact into the repository root, preserving the `backend/` and `frontend/` paths.
4. Review the resulting manifest/lock diffs and dependency licenses/security notices. The ML lock must preserve the same pins as the standard lock and only add the optional ML stack.
5. Commit the manifests and every refreshed lock together. Do not merge if the lock-validation step or any Windows/Linux Python test matrix job fails.

## Verification

CI runs `scripts/validate_dependency_locks.py` in each OS/Python matrix cell. The validator verifies that each direct manifest dependency is represented by a compatible exact pin, every locked package has a SHA-256 hash, the optional ML lock preserves the standard lock's exact pins, and the npm lock root matches `package.json`. The install test then installs Python dependencies with `pip install --require-hashes` and the frontend with `npm ci`.

The Windows installer records the selected lock/manifest fingerprints inside `.venv/`. If a manifest or selected lock changes in a later checkout/update, startup forces a dependency repair. It rejects a stale lock matrix instead of marking stale packages as successfully installed.

## Security and operations

A lock establishes repeatable resolution and artifact hashes; it is not proof that every dependency is safe, well-licensed, free from vulnerabilities, or compatible with the product. Review dependency advisories and license changes before refreshing. Keep optional speech-model weights and FFmpeg/FFprobe binaries separate from Python/npm lockfiles and review their distribution/license terms independently.
