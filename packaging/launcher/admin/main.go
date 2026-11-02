// SuiteAdmin.exe for Production Suite: asks Windows for administrator rights (manifest), then opens the
// server settings window (SuiteAdmin.ps1 in the same folder) with no console window. Standard library only.
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

const title = "Production Suite Admin"

func msg(text string, icon uintptr) {
	box := syscall.NewLazyDLL("user32.dll").NewProc("MessageBoxW")
	t, _ := syscall.UTF16PtrFromString(text)
	c, _ := syscall.UTF16PtrFromString(title)
	box.Call(0, uintptr(unsafe.Pointer(t)), uintptr(unsafe.Pointer(c)), icon)
}

func exists(p string) bool { _, err := os.Stat(p); return err == nil }

func main() {
	exe, err := os.Executable()
	if err != nil {
		msg("Production Suite Admin couldn't find its own folder: "+err.Error(), 0x10)
		os.Exit(1)
	}
	dir := filepath.Dir(exe)
	script := filepath.Join(dir, "SuiteAdmin.ps1")
	if !exists(script) {
		msg("SuiteAdmin.ps1 is missing from "+dir+". Run Setup.exe to repair the install.", 0x30)
		os.Exit(1)
	}
	ps := filepath.Join(os.Getenv("SystemRoot"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
	if !exists(ps) {
		ps = "powershell.exe"
	}
	cmd := exec.Command(ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", script)
	cmd.Dir = filepath.Dir(dir)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
	if err := cmd.Run(); err != nil {
		detail := strings.TrimSpace(stderr.String())
		if len(detail) > 900 {
			detail = detail[:900] + "..."
		}
		text := "The admin window stopped unexpectedly (" + err.Error() + ")."
		if detail != "" {
			text += "\n\n" + detail
		}
		text += "\n\nIts log is logs\\admin.log in the suite's folder. If your company blocks PowerShell scripts, ask IT to allow this one."
		msg(text, 0x10)
		os.Exit(1)
	}
}
