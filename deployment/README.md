# Optional local user service

[projectclarity.service](projectclarity.service) is an example systemd user unit,
not a public-hosting configuration. It assumes the checkout is at
`~/ProjectClarity`, with its virtual environment installed and a private `.env`.
Edit the working directory and executable/credential paths if your checkout differs.
`%h` expands to the user's home directory; no developer-specific path is required.

After checking the paths, install from the project root:

```bash
mkdir -p ~/.config/systemd/user
cp deployment/projectclarity.service ~/.config/systemd/user/projectclarity.service
systemctl --user daemon-reload
systemctl --user enable --now projectclarity.service
```

The interface remains at http://127.0.0.1:8775/. Inspect status with
`systemctl --user status projectclarity.service`. Stop active research in the UI
before restarting the service; interrupted work is not automatically replayed.
The unit uses a restrictive file-creation mask, but it does not add authentication
or a public reverse proxy. Keep credentials and saved research outside Git.
