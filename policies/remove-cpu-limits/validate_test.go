package main

import (
	"bytes"
	"encoding/json"
	"os"
	"testing"

	kubewarden_protocol "github.com/kubewarden/policy-sdk-go/protocol"
)

// request builds the policy payload from an admission request fixture, after an optional edit.
func request(t *testing.T, fixture string, settings Settings, edit func(req map[string]any)) []byte {
	t.Helper()
	raw, err := os.ReadFile(fixture)
	if err != nil {
		t.Fatalf("cannot read %s: %v", fixture, err)
	}
	var req map[string]any
	if err := json.Unmarshal(raw, &req); err != nil {
		t.Fatalf("cannot parse %s: %v", fixture, err)
	}
	if edit != nil {
		edit(req)
	}
	settingsJSON, err := json.Marshal(settings)
	if err != nil {
		t.Fatalf("cannot marshal settings: %v", err)
	}
	payload, err := json.Marshal(map[string]any{"request": req, "settings": json.RawMessage(settingsJSON)})
	if err != nil {
		t.Fatalf("cannot marshal request: %v", err)
	}
	return payload
}

func run(t *testing.T, payload []byte) kubewarden_protocol.ValidationResponse {
	t.Helper()
	responsePayload, err := validate(payload)
	if err != nil {
		t.Fatalf("validate returned an error: %v", err)
	}
	var response kubewarden_protocol.ValidationResponse
	decoder := json.NewDecoder(bytes.NewReader(responsePayload))
	decoder.UseNumber() // numbers in the mutated object come back as json.Number, as the policy wrote them
	if err := decoder.Decode(&response); err != nil {
		t.Fatalf("cannot parse response %s: %v", responsePayload, err)
	}
	return response
}

func mutated(t *testing.T, response kubewarden_protocol.ValidationResponse) map[string]any {
	t.Helper()
	if !response.Accepted {
		message := ""
		if response.Message != nil {
			message = *response.Message
		}
		t.Fatalf("request rejected: %s", message)
	}
	object, ok := response.MutatedObject.(map[string]any)
	if !ok {
		t.Fatalf("expected a mutated object, got %#v", response.MutatedObject)
	}
	return object
}

func acceptedUnchanged(t *testing.T, response kubewarden_protocol.ValidationResponse) {
	t.Helper()
	if !response.Accepted {
		t.Fatal("request rejected")
	}
	if response.MutatedObject != nil {
		t.Fatalf("expected no mutation, got %#v", response.MutatedObject)
	}
}

// container returns a container of the pod spec of the mutated object.
func container(t *testing.T, object map[string]any, kind, list string, index int) map[string]any {
	t.Helper()
	path := append(podSpecPath(kind), list)
	containers, ok := dig(object, path...).([]any)
	if !ok || index >= len(containers) {
		t.Fatalf("no %s[%d] at %v", list, index, path)
	}
	c, ok := containers[index].(map[string]any)
	if !ok {
		t.Fatalf("%s[%d] is not an object", list, index)
	}
	return c
}

func resources(t *testing.T, c map[string]any) (requests, limits map[string]any, hasLimits bool) {
	t.Helper()
	res, _ := c["resources"].(map[string]any)
	requests, _ = res["requests"].(map[string]any)
	limits, hasLimits = res["limits"].(map[string]any)
	return requests, limits, hasLimits
}

func TestRemovesCPULimitsFromPod(t *testing.T) {
	object := mutated(t, run(t, request(t, "test_data/pod_with_cpu_limit.json", Settings{}, nil)))

	// app: CPU limit gone, memory limit and both requests kept, limit written as a number stays a number
	requests, limits, _ := resources(t, container(t, object, "Pod", "containers", 0))
	if _, ok := limits["cpu"]; ok {
		t.Error("app: CPU limit still set")
	}
	if limits["memory"] != "256Mi" || requests["cpu"] != "250m" || requests["memory"] != "128Mi" {
		t.Errorf("app: unexpected resources: requests=%v limits=%v", requests, limits)
	}

	// sidecar: had no requests, gets the old limit as CPU request
	requests, limits, _ = resources(t, container(t, object, "Pod", "containers", 1))
	if _, ok := limits["cpu"]; ok {
		t.Error("sidecar: CPU limit still set")
	}
	if requests["cpu"] != "500m" || limits["memory"] != "64Mi" {
		t.Errorf("sidecar: unexpected resources: requests=%v limits=%v", requests, limits)
	}

	// pause: no resources, untouched
	if _, ok := container(t, object, "Pod", "containers", 2)["resources"]; ok {
		t.Error("pause: resources added")
	}

	// init container: its only limit was CPU, so the limits block disappears
	requests, _, hasLimits := resources(t, container(t, object, "Pod", "initContainers", 0))
	if hasLimits {
		t.Error("init: limits block still present")
	}
	if requests["cpu"] != "100m" || requests["memory"] != "64Mi" {
		t.Errorf("init: unexpected requests: %v", requests)
	}

	// Other fields survive the round trip as written.
	if grace, _ := dig(object, "spec", "terminationGracePeriodSeconds").(json.Number); grace != "30" {
		t.Errorf("terminationGracePeriodSeconds changed: %#v", dig(object, "spec", "terminationGracePeriodSeconds"))
	}
	if dig(object, "metadata", "labels", "app") != "app" {
		t.Error("labels changed")
	}
}

func TestNumericLimitBecomesNumericRequest(t *testing.T) {
	object := mutated(t, run(t, request(t, "test_data/pod_with_cpu_limit.json", Settings{}, func(req map[string]any) {
		c := dig(req, "object", "spec", "containers").([]any)[0].(map[string]any)
		c["resources"] = map[string]any{"limits": map[string]any{"cpu": 2}}
	})))
	requests, _, hasLimits := resources(t, container(t, object, "Pod", "containers", 0))
	if hasLimits {
		t.Error("limits block still present")
	}
	if cpu, _ := requests["cpu"].(json.Number); cpu != "2" {
		t.Errorf("expected numeric request 2, got %#v", requests["cpu"])
	}
}

func TestAcceptsPodWithoutCPULimit(t *testing.T) {
	acceptedUnchanged(t, run(t, request(t, "test_data/pod_without_cpu_limit.json", Settings{}, nil)))
}

func TestIgnoresImages(t *testing.T) {
	settings := Settings{IgnoreImages: []string{"registry.example.com/platform/*", "registry.example.com/team-alpha/init:1.0"}}
	object := mutated(t, run(t, request(t, "test_data/pod_with_cpu_limit.json", settings, nil)))

	if _, limits, _ := resources(t, container(t, object, "Pod", "containers", 0)); limits["cpu"] != nil {
		t.Error("app: CPU limit still set")
	}
	if _, limits, _ := resources(t, container(t, object, "Pod", "containers", 1)); limits["cpu"] != "500m" {
		t.Errorf("sidecar: CPU limit should be kept (prefix pattern), limits=%v", limits)
	}
	if _, limits, _ := resources(t, container(t, object, "Pod", "initContainers", 0)); limits["cpu"] != "200m" {
		t.Errorf("init: CPU limit should be kept (exact pattern), limits=%v", limits)
	}
}

func TestAllImagesIgnoredMeansNoMutation(t *testing.T) {
	settings := Settings{IgnoreImages: []string{"registry.example.com/*"}}
	acceptedUnchanged(t, run(t, request(t, "test_data/pod_with_cpu_limit.json", settings, nil)))
}

func TestKeepAnnotationOnPod(t *testing.T) {
	acceptedUnchanged(t, run(t, request(t, "test_data/pod_with_cpu_limit.json", Settings{}, func(req map[string]any) {
		dig(req, "object", "metadata").(map[string]any)["annotations"] = map[string]any{defaultKeepAnnotation: "true"}
	})))
	// Any value other than "true" does not opt out.
	mutated(t, run(t, request(t, "test_data/pod_with_cpu_limit.json", Settings{}, func(req map[string]any) {
		dig(req, "object", "metadata").(map[string]any)["annotations"] = map[string]any{defaultKeepAnnotation: "yes"}
	})))
}

func TestKeepAnnotationOnPodTemplate(t *testing.T) {
	settings := Settings{KeepAnnotation: "example.com/pinned-cpu"}
	acceptedUnchanged(t, run(t, request(t, "test_data/deployment_with_cpu_limit.json", settings, func(req map[string]any) {
		dig(req, "object", "spec", "template", "metadata").(map[string]any)["annotations"] = map[string]any{"example.com/pinned-cpu": "true"}
	})))
	// The default annotation is not honoured once another one is configured.
	mutated(t, run(t, request(t, "test_data/deployment_with_cpu_limit.json", settings, func(req map[string]any) {
		dig(req, "object", "spec", "template", "metadata").(map[string]any)["annotations"] = map[string]any{defaultKeepAnnotation: "true"}
	})))
}

func TestRemovesCPULimitsFromDeploymentTemplate(t *testing.T) {
	object := mutated(t, run(t, request(t, "test_data/deployment_with_cpu_limit.json", Settings{}, nil)))
	requests, limits, _ := resources(t, container(t, object, "Deployment", "containers", 0))
	if _, ok := limits["cpu"]; ok {
		t.Error("CPU limit still set")
	}
	if limits["memory"] != "512Mi" || requests["cpu"] != "500m" {
		t.Errorf("unexpected resources: requests=%v limits=%v", requests, limits)
	}
	if replicas, _ := dig(object, "spec", "replicas").(json.Number); replicas != "3" {
		t.Errorf("replicas changed: %#v", dig(object, "spec", "replicas"))
	}
}

func TestRemovesCPULimitsFromCronJobTemplate(t *testing.T) {
	object := mutated(t, run(t, request(t, "test_data/cronjob_with_cpu_limit.json", Settings{}, nil)))
	requests, limits, _ := resources(t, container(t, object, "CronJob", "containers", 0))
	if _, ok := limits["cpu"]; ok {
		t.Error("CPU limit still set")
	}
	if requests["cpu"] != "1" || limits["memory"] != "1Gi" {
		t.Errorf("unexpected resources: requests=%v limits=%v", requests, limits)
	}
}

func TestPodUpdateIsLeftAlone(t *testing.T) {
	acceptedUnchanged(t, run(t, request(t, "test_data/pod_with_cpu_limit.json", Settings{}, func(req map[string]any) {
		req["operation"] = "UPDATE"
	})))
}

func TestOtherKindsAreAccepted(t *testing.T) {
	acceptedUnchanged(t, run(t, request(t, "test_data/pod_with_cpu_limit.json", Settings{}, func(req map[string]any) {
		req["kind"] = map[string]any{"group": "", "version": "v1", "kind": "ConfigMap"}
	})))
}

func TestInvalidObjectIsRejected(t *testing.T) {
	response := run(t, request(t, "test_data/pod_with_cpu_limit.json", Settings{}, func(req map[string]any) {
		req["object"] = "not an object"
	}))
	if response.Accepted {
		t.Error("expected a rejection")
	}
}

func TestSettingsValidation(t *testing.T) {
	cases := []struct {
		name    string
		payload string
		valid   bool
	}{
		{"empty", `{}`, true},
		{"patterns", `{"ignoreImages": ["registry.example.com/platform/*", "busybox:1.36"]}`, true},
		{"custom annotation", `{"keepAnnotation": "example.com/keep"}`, true},
		{"empty pattern", `{"ignoreImages": [""]}`, false},
		{"wildcard in the middle", `{"ignoreImages": ["registry.example.com/*/app"]}`, false},
		{"annotation with space", `{"keepAnnotation": "example.com/keep me"}`, false},
		{"not json", `nope`, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			responsePayload, err := validateSettings([]byte(c.payload))
			if err != nil {
				t.Fatalf("validateSettings returned an error: %v", err)
			}
			var response kubewarden_protocol.SettingsValidationResponse
			if err := json.Unmarshal(responsePayload, &response); err != nil {
				t.Fatalf("cannot parse response %s: %v", responsePayload, err)
			}
			if response.Valid != c.valid {
				t.Errorf("valid=%v, expected %v (%s)", response.Valid, c.valid, responsePayload)
			}
		})
	}
}

func TestDefaultsAppliedToRequestSettings(t *testing.T) {
	req := kubewarden_protocol.ValidationRequest{Settings: json.RawMessage(`{}`)}
	settings, err := NewSettingsFromValidationReq(&req)
	if err != nil {
		t.Fatal(err)
	}
	if settings.KeepAnnotation != defaultKeepAnnotation {
		t.Errorf("default annotation not applied: %q", settings.KeepAnnotation)
	}
	req.Settings = nil
	if _, err := NewSettingsFromValidationReq(&req); err != nil {
		t.Errorf("empty settings should be accepted: %v", err)
	}
}
