#!/usr/bin/env bats
# Runs the annotated policy with kwctl against the admission requests in test_data/.

patch_of() {
  # Decoded JSON patch of a kwctl run output (kwctl may log lines before the JSON response)
  echo "$1" | grep '^{' | jq -r '.patch' | base64 -d
}

@test "removes the CPU limits of a pod" {
  run kwctl run annotated-policy.wasm -r test_data/pod_with_cpu_limit.json
  echo "output = ${output}"
  [ "$status" -eq 0 ]
  [ $(expr "$output" : '.*"allowed": *true') -ne 0 ]
  [ $(expr "$output" : '.*"patchType": *"JSONPatch"') -ne 0 ]
  patch=$(patch_of "$output")
  echo "patch = ${patch}"
  # app keeps its memory limit: only limits/cpu goes
  [ $(expr "$patch" : '.*"remove".*"/spec/containers/0/resources/limits/cpu"') -ne 0 ]
  # the init container had only a CPU limit: the whole limits block goes
  [ $(expr "$patch" : '.*"/spec/initContainers/0/resources/limits"') -ne 0 ]
  # the sidecar had no request: it gets the old limit
  [ $(expr "$patch" : '.*"/spec/containers/1/resources/requests".*500m') -ne 0 ]
}

@test "leaves a pod without CPU limits alone" {
  run kwctl run annotated-policy.wasm -r test_data/pod_without_cpu_limit.json
  echo "output = ${output}"
  [ "$status" -eq 0 ]
  [ $(expr "$output" : '.*"allowed": *true') -ne 0 ]
  [ $(expr "$output" : '.*"patch"') -eq 0 ]
}

@test "ignores images from ignoreImages" {
  run kwctl run annotated-policy.wasm -r test_data/pod_with_cpu_limit.json \
    --settings-json '{"ignoreImages": ["registry.example.com/*"]}'
  echo "output = ${output}"
  [ "$status" -eq 0 ]
  [ $(expr "$output" : '.*"allowed": *true') -ne 0 ]
  [ $(expr "$output" : '.*"patch"') -eq 0 ]
}

@test "removes the CPU limits of a deployment's pod template" {
  run kwctl run annotated-policy.wasm -r test_data/deployment_with_cpu_limit.json
  echo "output = ${output}"
  [ "$status" -eq 0 ]
  [ $(expr "$output" : '.*"allowed": *true') -ne 0 ]
  patch=$(patch_of "$output")
  echo "patch = ${patch}"
  [ $(expr "$patch" : '.*"/spec/template/spec/containers/0/resources/limits/cpu"') -ne 0 ]
}

@test "rejects invalid settings" {
  run kwctl run annotated-policy.wasm -r test_data/pod_with_cpu_limit.json \
    --settings-json '{"ignoreImages": ["registry.example.com/*/app"]}'
  echo "output = ${output}"
  [ "$status" -ne 0 ]
}
