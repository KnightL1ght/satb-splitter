using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

[assembly: AssemblyTitle("SATB Splitter")]
[assembly: AssemblyDescription("SATB Splitter Studio — Choral PDF & MusicXML to Soprano, Alto, Tenor, Bass")]
[assembly: AssemblyConfiguration("")]
[assembly: AssemblyCompany("SATBSplitter")]
[assembly: AssemblyProduct("SATB Splitter")]
[assembly: AssemblyCopyright("Copyright © 2026")]
[assembly: AssemblyVersion("1.2.0.0")]
[assembly: AssemblyFileVersion("1.2.0.0")]

namespace SatbSplitter
{
    internal static class Program
    {
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

        private const uint MB_ICONERROR = 0x00000010;

        private static string ResolveProjectRoot()
        {
            string exeDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(
                Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar
            );

            string[] candidates = new string[]
            {
                exeDir,
                Path.GetFullPath(Path.Combine(exeDir, "..")),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "satb-splitter")
            };

            foreach (string candidate in candidates)
            {
                string pyw = Path.Combine(candidate, ".venv", "Scripts", "pythonw.exe");
                string py = Path.Combine(candidate, ".venv", "Scripts", "python.exe");
                string src = Path.Combine(candidate, "src", "satb_splitter");
                if ((File.Exists(pyw) || File.Exists(py)) && Directory.Exists(src))
                {
                    return candidate;
                }
            }
            return exeDir;
        }

        private static string QuoteArg(string arg)
        {
            if (string.IsNullOrEmpty(arg))
                return "\"\"";
            if (arg.IndexOfAny(new char[] { ' ', '\t', '\"' }) < 0)
                return arg;
            return "\"" + arg.Replace("\"", "\\\"") + "\"";
        }

        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                string root = ResolveProjectRoot();
                string pythonw = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
                string python = Path.Combine(root, ".venv", "Scripts", "python.exe");
                string interpreter = File.Exists(pythonw) ? pythonw : python;

                if (!File.Exists(interpreter))
                {
                    MessageBoxW(
                        IntPtr.Zero,
                        "Could not find the SATB Splitter Python environment at:\n" + interpreter,
                        "SATB Splitter",
                        MB_ICONERROR
                    );
                    return 1;
                }

                StringBuilder argBuilder = new StringBuilder("-m satb_splitter.gui.app");
                if (args != null)
                {
                    foreach (string a in args)
                    {
                        argBuilder.Append(' ').Append(QuoteArg(a));
                    }
                }

                ProcessStartInfo psi = new ProcessStartInfo
                {
                    FileName = interpreter,
                    Arguments = argBuilder.ToString(),
                    WorkingDirectory = root,
                    UseShellExecute = false,
                    CreateNoWindow = true
                };

                string srcDir = Path.Combine(root, "src");
                string existingPyPath = Environment.GetEnvironmentVariable("PYTHONPATH");
                psi.EnvironmentVariables["PYTHONPATH"] = string.IsNullOrEmpty(existingPyPath)
                    ? srcDir
                    : (srcDir + Path.PathSeparator + existingPyPath);

                if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("SATB_MSCZ")))
                {
                    psi.EnvironmentVariables["SATB_MSCZ"] = "1";
                }

                string bundledExe = Path.Combine(root, "Audiveris", "Audiveris.exe");
                string dev3lExe = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                    "Audiveris-local-dev3l",
                    "Audiveris.exe"
                );
                if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("SATB_AUDIVERIS")))
                {
                    if (File.Exists(bundledExe))
                    {
                        psi.EnvironmentVariables["SATB_AUDIVERIS"] = bundledExe;
                    }
                    else if (File.Exists(dev3lExe))
                    {
                        psi.EnvironmentVariables["SATB_AUDIVERIS"] = dev3lExe;
                    }
                }

                Process.Start(psi);
                return 0;
            }
            catch (Exception ex)
            {
                MessageBoxW(
                    IntPtr.Zero,
                    "Failed to launch SATB Splitter:\n" + ex.Message,
                    "SATB Splitter",
                    MB_ICONERROR
                );
                return 1;
            }
        }
    }
}
