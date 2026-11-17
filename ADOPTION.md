# Adoption and release checklist

The executor remains in its own repository. The gVisor monorepo contains only
the user guide, Bazel document registration, and captures from a real run.

Before official adoption, the repository owner and gVisor maintainers must agree
on the license, attribution, and maintainer ownership. No license has been
assigned by this change. Confirm that the owner can license the existing code
and assets before choosing and adding the organization-approved license.

The current owner must transfer the existing repository to `gvisor/gvisor-demo`,
with a receiving organization administrator accepting the transfer as required.
Preserve commit history, issues and redirects. This local implementation does
not perform the transfer, push commits, send messages, or deploy the site.

After transfer:

1. Set repository and branch protection policies; assign maintainers and CI
   permissions. Provision the disposable integration runner before enabling
   its manual workflow.
2. Update clone commands, README links and the gVisor demo guide from
   `milantracy/gvisor-demo` to `gvisor/gvisor-demo`.
3. Validate cloning the new URL, the old URL's redirect, unit CI, and the
   privileged integration suite on a freshly provisioned Ubuntu 24.04 x86_64 VM.
4. Merge and release the executor changes before publishing the guide that
   documents the new options. The source commit used for this implementation
   is d6414bb9734dc00ad0af88b96594210d81997ba8.
5. Confirm the guide is present at `/docs/user_guide/demos/`, its images load,
   and every installation and execution command works on a clean machine.

The complete systemd/Chromium scene and cold-cache/high-density demonstrations
need a separate manual run on a disposable machine with Docker and network
access. The initial quick workflow does not depend on these features. ARM64
support is not part of the first validated release.
