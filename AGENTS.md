# Print Handy: where to work

- **Dysprosium (`/home/dysprosium/projects/print-handy`)** is a development checkout only. Use it to read and edit code, commit, and push PR branches. It is not the live deployment or a test host. Do not run Print Handy tests here, even a focused test.
- **GitHub Actions (`catduckgnaf/print-handy`)** is the preferred place to run CI. Run full or slow suites there. Push your PR branch and check CI on the exact commit being reviewed.
- **Francium** is the test host when you need to run tests outside GitHub CI. Use `ssh francium` and the `~/print-handy-test` checkout. If Francium is unreachable, use GitHub CI; do not fall back to Dysprosium.
- **Uranium (`192.168.250.135`)** runs the live Unraid Docker container named `print-handy`. For read-only live diagnostics, use `ssh root@192.168.250.135 docker logs print-handy`. Do not use the live container as a test environment.
