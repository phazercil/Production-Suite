// SuiteAdmin.exe for Production Suite: opens the server settings window (SuiteAdmin.ps1 in the same folder),
// or with --tray the Production Suite tray icon. Runs as the signed-in user: no administrator rights are
// needed, because the suite's control service does the privileged work. Standard library only.
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
	tray := false
	for _, a := range os.Args[1:] {
		if strings.EqualFold(a, "--tray") || strings.EqualFold(a, "/tray") {
			tray = true
		}
	}
	exe, err := os.Executable()
	if err != nil {
		msg("Production Suite Admin couldn't find its own folder: "+err.Error(), 0x10)
		os.Exit(1)
	}
	dir := filepath.Dir(exe)
	script := filepath.Join(dir, "SuiteAdmin.ps1")
	if !exists(script) {
		if !tray {
			msg("SuiteAdmin.ps1 is missing from "+dir+", or this Windows account can't read it. Run Setup.exe to repair the install.", 0x30)
		}
		os.Exit(1)
	}
	ps := filepath.Join(os.Getenv("SystemRoot"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
	if !exists(ps) {
		ps = "powershell.exe"
	}
	args := []string{"-NoProfile", "-ExecutionPolicy", "Bypass", "-STA", "-File", script}
	if tray {
		args = append(args, "-Tray")
	}
	cmd := exec.Command(ps, args...)
	cmd.Dir = dir
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	// CREATE_NO_WINDOW: PowerShell gets no console window at all. The window it opens is shown normally
	// (no "start hidden" flag, which Windows would also apply to the first window the script opens).
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000}
	if err := cmd.Run(); err != nil {
		detail := strings.TrimSpace(stderr.String())
		if len(detail) > 900 {
			detail = detail[:900] + "..."
		}
		text := "Production Suite Admin stopped unexpectedly (" + err.Error() + ")."
		if detail != "" {
			text += "\n\n" + detail
		}
		text += "\n\nIts log is in " + filepath.Join(os.Getenv("LOCALAPPDATA"), "ProductionSuite") + ". If your company blocks PowerShell scripts, ask IT to allow this one."
		msg(text, 0x10)
		os.Exit(1)
	}
}
