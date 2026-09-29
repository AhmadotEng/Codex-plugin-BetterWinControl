using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Interop;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        Window Make(string title, int x)
        {
            var text = new TextBox { Text = "owned lease fixture", Margin = new Thickness(20) };
            AutomationProperties.SetAutomationId(text, "fixture-input");
            return new Window { Title = title, Content = text, Width = 260, Height = 140,
                Left = x, Top = 80, ShowActivated = false, WindowStartupLocation = WindowStartupLocation.Manual };
        }
        var primary = Make("Owned lease primary", 40);
        var independent = Make("Owned lease independent", 320);
        var dialog = Make("Owned lease dialog", 70);
        primary.Show();
        independent.Show();
        dialog.Owner = primary;
        dialog.Show();
        Console.WriteLine(JsonSerializer.Serialize(new { @event = "ready", fixturePID = Environment.ProcessId,
            primary = new WindowInteropHelper(primary).Handle.ToInt64(), independent = new WindowInteropHelper(independent).Handle.ToInt64(),
            ownedDialog = new WindowInteropHelper(dialog).Handle.ToInt64() }));
        Console.Out.Flush();
        new Thread(() =>
        {
            while (Console.ReadLine() is { } command)
            {
                if (command == "close-independent") app.Dispatcher.Invoke(() => independent.Close());
                else if (command == "close-dialog") app.Dispatcher.Invoke(() => dialog.Close());
                else if (command == "stop") { app.Dispatcher.Invoke(() => app.Shutdown()); return; }
            }
            app.Dispatcher.Invoke(() => app.Shutdown());
        }) { IsBackground = true }.Start();
        app.Run();
    }
}
