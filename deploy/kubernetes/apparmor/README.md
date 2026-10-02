# AppArmor for the trusted Bubblewrap supervisor

`core-agent-runtime-v1.profile` allows the trusted supervisor to create nested
user, mount, PID and network namespaces on hosts where AppArmor otherwise denies
those operations. The inner Bubblewrap filesystem/network boundaries and inner
seccomp filters remain mandatory for every untrusted command.

The operator loads this named profile on each eligible node before scheduling
the agent. Use the host's AppArmor parser and includes; compile with ABI 4.0
and reject rules the host cannot enforce. Loading profiles requires host
administration, independently of the agent. Existing runtime profiles must not
be replaced. A node reboot or replacement must restore this profile before
accepting agent workloads.

Select it in the agent container's security context:

```yaml
appArmorProfile:
  type: Localhost
  localhostProfile: core-agent-runtime-v1
```

The agent retains `hostUsers: false`, non-root UID/GID, dropped capabilities,
`allowPrivilegeEscalation: false`, a read-only root filesystem and the native
Localhost OCI seccomp profile. Profile installation permissions do not belong
to the running agent.

Some kernel/CSI combinations reject an idmapped host `/dev/net/tun` mount. On
those hosts, provision a TUN character device (major 10, minor 200) on a
dedicated CSI volume and mount only that device through `subPath`; the operator
initializer needs `MKNOD`, while the agent does not. Keep that device volume
separate from chat files. Do not switch off Pod user namespaces to work around
the mount failure.

This profile does not set the Pod PID limit or supply network test fixtures.
Verify the finite aggregate Pod PID limit and run the native sandbox gate on
the actual deployment. The CI limit of 512 is a dedicated test fixture; it is
not a requirement to change every cluster's existing finite limit to 512.
