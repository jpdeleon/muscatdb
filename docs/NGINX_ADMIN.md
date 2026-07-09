
sudo env "PATH=$PATH" uv run muscat-db htpasswd add <user>
uv run muscat-db restart --nginx --reload
