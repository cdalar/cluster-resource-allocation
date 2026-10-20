package main

import (
	"encoding/json"
	"fmt"
	"strings"

	kubewarden "github.com/kubewarden/policy-sdk-go"
	kubewarden_protocol "github.com/kubewarden/policy-sdk-go/protocol"
)

// defaultKeepAnnotation marks a pod (or a pod template) whose CPU limits must stay.
const defaultKeepAnnotation = "allocation.cluster-resource-allocation/keep-cpu-limits"

// Settings of the policy (the `settings:` block of the ClusterAdmissionPolicy).
type Settings struct {
	// Container images to leave alone: the whole image, or a prefix when the pattern ends with '*'.
	IgnoreImages []string `json:"ignoreImages,omitempty"`
	// Annotation that keeps the CPU limits of a pod or pod template when set to "true".
	KeepAnnotation string `json:"keepAnnotation,omitempty"`
}

// NewSettingsFromValidationReq reads the settings of a validation request, with defaults.
func NewSettingsFromValidationReq(req *kubewarden_protocol.ValidationRequest) (Settings, error) {
	settings := Settings{}
	if len(req.Settings) > 0 {
		if err := json.Unmarshal(req.Settings, &settings); err != nil {
			return settings, err
		}
	}
	settings.applyDefaults()
	return settings, settings.Valid()
}

func (s *Settings) applyDefaults() {
	if s.KeepAnnotation == "" {
		s.KeepAnnotation = defaultKeepAnnotation
	}
}

// Valid checks the settings.
func (s *Settings) Valid() error {
	for _, pattern := range s.IgnoreImages {
		if pattern == "" {
			return fmt.Errorf("ignoreImages: empty pattern")
		}
		if i := strings.Index(pattern, "*"); i >= 0 && i != len(pattern)-1 {
			return fmt.Errorf("ignoreImages: %q: '*' is only allowed at the end", pattern)
		}
	}
	if strings.ContainsAny(s.KeepAnnotation, " \t\r\n") {
		return fmt.Errorf("keepAnnotation: %q contains whitespace", s.KeepAnnotation)
	}
	return nil
}

// ignoresImage tells whether a container image matches one of the ignoreImages patterns.
func (s *Settings) ignoresImage(image string) bool {
	for _, pattern := range s.IgnoreImages {
		if strings.HasSuffix(pattern, "*") {
			if strings.HasPrefix(image, pattern[:len(pattern)-1]) {
				return true
			}
		} else if image == pattern {
			return true
		}
	}
	return false
}

// validateSettings is the waPC entry point Kubewarden calls when the policy is loaded.
func validateSettings(payload []byte) ([]byte, error) {
	settings := Settings{}
	if err := json.Unmarshal(payload, &settings); err != nil {
		return kubewarden.RejectSettings(kubewarden.Message(err.Error()))
	}
	settings.applyDefaults()
	if err := settings.Valid(); err != nil {
		return kubewarden.RejectSettings(kubewarden.Message(err.Error()))
	}
	return kubewarden.AcceptSettings()
}
