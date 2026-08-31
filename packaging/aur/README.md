# AUR release process

The stable package is named `link-studio`. Its source is the signed-version release archive from
`jweaver60/link-studio`.

Before publishing a version:

1. Push the matching `vX.Y.Z` Git tag and wait for the GitHub release workflow.
2. Download the public `link-studio-X.Y.Z.tar.gz` release asset and replace `SKIP` with its SHA-256.
3. Run `makepkg --printsrcinfo > .SRCINFO` from this directory.
4. Run `makepkg --cleanbuild --syncdeps` in a clean Arch environment.
5. Copy only `PKGBUILD`, `.SRCINFO`, and the AUR repository license into the AUR Git checkout.
6. Commit and push to `ssh://aur@aur.archlinux.org/link-studio.git`.

Do not submit while the upstream release archive requires GitHub authentication: an AUR package
must be buildable by an unauthenticated user.
