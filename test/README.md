# Test harness

An LXD-based container harness for iterating on `setup.sh` without touching
a real Raspberry Pi. Boots in seconds, supports Docker nesting, works
reliably on Ubuntu 24.04 hosts (where Multipass+QEMU is broken).

## Prerequisites

```bash
sudo snap install lxd
sudo lxd init --auto
sudo usermod -aG lxd $USER   # then log out / back in
```

(All `test/*.sh` scripts use `sudo lxc`, so they work even before group
membership has been re-loaded.)

## Scripts

| Script                  | What it does                                             |
| ----------------------- | -------------------------------------------------------- |
| `bash test/up.sh`       | Create the LXD container, push the repo, prep `pi` user. Idempotent. |
| `bash test/setup.sh`    | Run `./setup.sh` once inside, leave services running, print URLs you can open in a browser. |
| `bash test/verify.sh`   | Run `./setup.sh` twice and assert the second run is a no-op (idempotency check). |
| `bash test/down.sh`     | Destroy the container.                                    |

## Typical loops

**Run-and-poke (browser testing):**

```bash
bash test/up.sh        # ~30s on first launch, ~5s on reuse
bash test/setup.sh     # prints URLs at the end
# open http://<container-ip>/, :81, :2283, :3000 in your browser
bash test/down.sh
```

**Idempotency check (CI-style):**

```bash
bash test/up.sh
bash test/verify.sh    # PASS if setup.sh is truly idempotent
bash test/down.sh
```

**Drop into the container by hand:**

```bash
sudo lxc shell testpi                          # root
sudo lxc exec testpi -- sudo -iu pi bash       # as the pi user
```

## Caveats

- Container is Ubuntu 22.04 (override with `LXD_IMAGE=ubuntu:24.04` etc).
  Pi OS is Debian Bookworm; close enough for behavior testing.
- No external partitions are attached, so `setup_disks` takes the fallback
  path with `STORAGE_ROOT=$HOME` — exactly the path we want to harden.
- No physical printer; CUPS install succeeds but obviously can't print.
- The container runs with `security.privileged=true` + `security.nesting=true`
  so Docker-in-LXD works for the Immich and NPM stacks. This is fine for a
  throwaway test box, not appropriate for production.
- For a really fast inner loop without spinning up a container, use
  `./setup.sh --dry-run` to see the planned commands without executing.
