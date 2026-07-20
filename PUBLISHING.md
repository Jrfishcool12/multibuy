# Publishing multibuy on GitHub Releases

This repo is set up so that **pushing a version tag builds the Windows app in
the cloud and publishes it as a downloadable Release automatically.** You never
have to build the `.exe` on your own machine again.

The workflow lives in `.github/workflows/release.yml`. On a tag push it:

1. spins up a Windows machine on GitHub,
2. installs dependencies and runs the full test suite (a failing test stops the release),
3. builds `multibuy.exe` with PyInstaller (bundling the native Solana libs + certs),
4. builds `multibuy-setup.exe` with Inno Setup,
5. creates a GitHub Release and attaches both files.

Your users then download `multibuy-setup.exe` from the release page and run it.

---

## One-time setup

You need [Git](https://git-scm.com/download/win) installed and a free GitHub
account. Do this once, from inside the `multibuy` folder in PowerShell.

### 1. Create the repository on GitHub

Go to https://github.com/new and create a new **empty** repository named
`multibuy` (no README, no .gitignore, no license — the repo already has them).
Note whether you want it **Public** (anyone can see the source and download) or
**Private** (only you and people you invite). Releases on a private repo are
only downloadable by people with access, so for public distribution choose Public.

### 2. Push the code

Replace `YOUR-USERNAME` with your GitHub username:

```powershell
git init
git add .
git commit -m "multibuy v1.0.0"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/multibuy.git
git push -u origin main
```

If Git asks you to sign in, a browser window will pop up — approve it.

### 3. Cut your first release

```powershell
git tag v1.0.0
git push origin v1.0.0
```

That tag push is what triggers the build. Go to your repo's **Actions** tab to
watch it run (about 5–10 minutes). When it finishes, the **Releases** section of
your repo (right-hand sidebar on the repo home page) will have a **v1.0.0**
release with `multibuy-setup.exe` attached. That file's URL is your download
link — share it with anyone.

---

## Shipping an update later

Whenever you have new changes:

```powershell
git add .
git commit -m "describe what changed"
git push
git tag v1.0.1
git push origin v1.0.1
```

Bump the number each time (`v1.0.1`, `v1.1.0`, etc.) — tags can't be reused.
Add a matching entry to `CHANGELOG.md` before you tag so the release notes make
sense. GitHub also auto-generates notes from your commit messages.

---

## Testing the build without releasing

Go to the **Actions** tab → **Release** workflow → **Run workflow**. That builds
the app and attaches the `.exe` files to the run as "artifacts" (downloadable
from the run page) **without** publishing a public release. Good for a dry run.

---

## What your users will see (and how to smooth it)

Because the installer isn't code-signed yet:

- **SmartScreen**: Windows shows a blue "Windows protected your PC" warning on
  first run. Users click **More info → Run anyway**. A code-signing certificate
  removes this — see the main conversation for the cost/options.
- **Antivirus**: some engines may flag a PyInstaller crypto app as suspicious
  even though it's clean. Code-signing greatly reduces this.

Consider adding a short "Download & Install" note on the release describing the
SmartScreen step so first-timers aren't scared off.

---

## Safety reminder

The `.gitignore` is configured to keep real keys, vault files, and configs out
of the repo. **Before your first push, double-check `git status` does not list
`keys.txt`, `solana_keys.txt`, any `*.vault`, or a real `config.yaml`.** Only
the `*.example.*` templates should be committed. Never commit a private key.
