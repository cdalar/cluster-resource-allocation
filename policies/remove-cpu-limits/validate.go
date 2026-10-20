package main

import (
	"bytes"
	"encoding/json"
	"fmt"

	kubewarden "github.com/kubewarden/policy-sdk-go"
	kubewarden_protocol "github.com/kubewarden/policy-sdk-go/protocol"
)

// podSpecPath is the key path of the pod spec inside each supported kind, nil for other kinds.
func podSpecPath(kind string) []string {
	switch kind {
	case "Pod":
		return []string{"spec"}
	case "Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "ReplicationController", "Job":
		return []string{"spec", "template", "spec"}
	case "CronJob":
		return []string{"spec", "jobTemplate", "spec", "template", "spec"}
	}
	return nil
}

// validate is the waPC entry point Kubewarden calls for every admission request.
//
// The object is handled as plain JSON (not typed structs), so fields this policy does not know
// about survive the mutation unchanged: Kubewarden turns the difference between the request's
// object and the returned object into a JSON patch.
func validate(payload []byte) ([]byte, error) {
	req := kubewarden_protocol.ValidationRequest{}
	if err := json.Unmarshal(payload, &req); err != nil {
		return kubewarden.RejectRequest(kubewarden.Message(err.Error()), kubewarden.Code(400))
	}
	settings, err := NewSettingsFromValidationReq(&req)
	if err != nil {
		return kubewarden.RejectRequest(kubewarden.Message(err.Error()), kubewarden.Code(400))
	}

	kind := req.Request.Kind.Kind
	specPath := podSpecPath(kind)
	if specPath == nil {
		return kubewarden.AcceptRequest() // no pod template in this kind
	}
	// The resources of a running pod cannot be changed through a pod update; leave updates alone.
	if kind == "Pod" && req.Request.Operation != "CREATE" {
		return kubewarden.AcceptRequest()
	}

	var object map[string]any
	decoder := json.NewDecoder(bytes.NewReader(req.Request.Object))
	decoder.UseNumber() // keep numbers as written (no float64 round trip)
	if err := decoder.Decode(&object); err != nil {
		return kubewarden.RejectRequest(kubewarden.Message(fmt.Sprintf("cannot decode %s: %v", kind, err)), kubewarden.Code(400))
	}

	// Opt-out annotation on the object itself or on its pod template.
	templateMetadata := make([]string, 0, len(specPath))
	templateMetadata = append(templateMetadata, specPath[:len(specPath)-1]...)
	templateMetadata = append(templateMetadata, "metadata")
	if hasAnnotation(object, []string{"metadata"}, settings.KeepAnnotation) ||
		hasAnnotation(object, templateMetadata, settings.KeepAnnotation) {
		return kubewarden.AcceptRequest()
	}

	spec, ok := dig(object, specPath...).(map[string]any)
	if !ok {
		return kubewarden.AcceptRequest()
	}
	if !removeCPULimits(spec, &settings) {
		return kubewarden.AcceptRequest()
	}
	return kubewarden.MutateRequest(object)
}

// removeCPULimits drops limits.cpu from every container and init container of a pod spec.
// A container without a CPU request gets the old limit as its request, which is what Kubernetes
// would have given it anyway (requests default to limits), so its scheduling guarantee is kept.
// Returns whether anything changed.
func removeCPULimits(spec map[string]any, settings *Settings) bool {
	changed := false
	for _, list := range []string{"initContainers", "containers"} {
		containers, _ := spec[list].([]any)
		for _, item := range containers {
			container, ok := item.(map[string]any)
			if !ok {
				continue
			}
			image, _ := container["image"].(string)
			if settings.ignoresImage(image) {
				continue
			}
			resources, ok := container["resources"].(map[string]any)
			if !ok {
				continue
			}
			limits, ok := resources["limits"].(map[string]any)
			if !ok {
				continue
			}
			limit, ok := limits["cpu"]
			if !ok {
				continue
			}
			delete(limits, "cpu")
			if len(limits) == 0 {
				delete(resources, "limits")
			}
			requests, ok := resources["requests"].(map[string]any)
			if !ok {
				requests = map[string]any{}
				resources["requests"] = requests
			}
			if _, ok := requests["cpu"]; !ok {
				requests["cpu"] = limit
			}
			changed = true
		}
	}
	return changed
}

// dig returns the value at a key path in nested JSON objects, or nil.
func dig(object map[string]any, path ...string) any {
	var current any = object
	for _, key := range path {
		m, ok := current.(map[string]any)
		if !ok {
			return nil
		}
		if current, ok = m[key]; !ok {
			return nil
		}
	}
	return current
}

// hasAnnotation tells whether the metadata at metadataPath carries the annotation set to "true".
func hasAnnotation(object map[string]any, metadataPath []string, name string) bool {
	metadata, _ := dig(object, metadataPath...).(map[string]any)
	annotations, _ := metadata["annotations"].(map[string]any)
	value, _ := annotations[name].(string)
	return value == "true"
}
