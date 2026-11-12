SSH identity for a lab tools instance with the `ssh` integration (docs/labtools.md). Not committed.

    ssh-keygen -t ed25519 -N "" -C agent-hq-labtools -f id_ed25519
    ssh-keyscan dhcp.home.arpa dns.home.arpa graylog.home.arpa > known_hosts
    sudo chown 10001 id_ed25519 && chmod 600 id_ed25519   # the container runs as UID 10001

Put id_ed25519.pub in ~agent-hq/.ssh/authorized_keys on each host in the inventory's ssh_hosts
(or the `user` you name per host), with a sudoers line for exactly the restarts you allow.
