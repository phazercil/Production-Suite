// Setup.exe for Production Suite: asks Windows for administrator rights (manifest), then opens the
// setup window (installer\setup-gui.ps1) with no console window. Only the Go standard library is used.
package main

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"
)

const title = "Production Suite setup"

func msg(text string, icon uintptr) {
	user32 := syscall.NewLazyDLL("user32.dll")
	box := user32.NewProc("MessageBoxW")
	t, _ := syscall.UTF16PtrFromString(text)
	c, _ := syscall.UTF16PtrFromString(title)
	box.Call(0, uintptr(unsafe.Pointer(t)), uintptr(unsafe.Pointer(c)), icon)
}

const (
	iconError = 0x10
	iconWarn  = 0x30
)

func exists(p string) bool { _, err := os.Stat(p); return err == nil }

func main() {
	exe, err := os.Executable()
	if err != nil {
		msg("Setup couldn't find its own folder: "+err.Error(), iconError)
		os.Exit(1)
	}
	dir := filepath.Dir(exe)
	script := filepath.Join(dir, "installer", "setup-gui.ps1")
	if !exists(script) || !exists(filepath.Join(dir, "app", "manage.py")) {
		msg("Setup is missing its files. Extract the whole download first (right-click the zip > Extract All), "+
			"then run Setup.exe from the extracted folder.", iconWarn)
		os.Exit(1)
	}
	ps := filepath.Join(os.Getenv("SystemRoot"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
	if !exists(ps) {
		ps = "powershell.exe"
	}
	cmd := exec.Command(ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", script)
	cmd.Dir = dir
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
	if err := cmd.Run(); err != nil {
		detail := strings.TrimSpace(stderr.String())
		if len(detail) > 900 {
			detail = detail[:900] + "..."
		}
		text := "The setup window stopped unexpectedly (" + err.Error() + ")."
		if detail != "" {
			text += "\n\n" + detail
		}
		text += "\n\nThe setup log is in " + filepath.Join(os.Getenv("ProgramData"), "ProductionSuite-install.log") +
			". If your company blocks PowerShell scripts, ask IT to allow this installer, or use the text-mode " +
			"installer: installer\\install.ps1."
		msg(text, iconError)
		os.Exit(1)
	}
}
