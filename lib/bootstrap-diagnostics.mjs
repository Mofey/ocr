// Only accept static bootstrap stages. Never forward raw VM/SDK error output.
const failure = /^OCR_SANDBOX_BOOT_FAILED stage=(configure_cgroups|check_cgroup_hierarchy|check_cgroup_type|disable_cgroup_threading|migrate_cgroup_processes|enable_cgroup_controllers|verify_cgroup_controllers|start_docker|disable_swap|mount_job_tmpfs|check_parser_image|restricted_container_probe) exit=([0-9]{1,3})(?: cgroup_mode=(root|domain|domain_threaded|threaded|invalid|unknown))?$/m;

export function bootstrapFailureDetails(stderr, exitCode) {
  const match = typeof stderr === 'string' ? stderr.match(failure) : null;
  return {
    stage: match?.[1] ?? 'unknown',
    ...(match?.[3] ? { cgroupMode: match[3] } : {}),
    ...(Number.isInteger(exitCode) && exitCode >= 0 && exitCode <= 255 ? { exitCode } : {}),
  };
}
