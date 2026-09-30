using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;

namespace WindowsBackgroundControlFixture;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        var app = new Application { ShutdownMode = ShutdownMode.OnMainWindowClose };
        var fixture = new Window
        {
            Title = "Windows Background Control - Test Fixture",
            Width = 520,
            Height = 330,
            Left = 90,
            Top = 90,
            ShowActivated = false,
            Background = Brushes.White,
            WindowStartupLocation = WindowStartupLocation.Manual
        };
        AutomationProperties.SetAutomationId(fixture, "fixture-window");
        var panel = new StackPanel { Margin = new Thickness(24) };
        panel.Children.Add(new TextBlock
        {
            Text = "Owned integration test fixture",
            FontSize = 21,
            Foreground = Brushes.DarkBlue,
            Margin = new Thickness(0, 0, 0, 14)
        });
        var input = new TextBox { Text = "ready", FontSize = 16, Margin = new Thickness(0, 0, 0, 12) };
        AutomationProperties.SetAutomationId(input, "fixture-input");
        AutomationProperties.SetName(input, "Fixture input");
        panel.Children.Add(input);
        var apply = new Button
        {
            Content = "Apply fixture value",
            Padding = new Thickness(12, 7, 12, 7),
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(0, 0, 0, 12)
        };
        AutomationProperties.SetAutomationId(apply, "fixture-apply");
        AutomationProperties.SetName(apply, "Apply fixture value");
        panel.Children.Add(apply);
        var status = new TextBlock { Text = "Applied 0: ready", FontSize = 16, TextWrapping = TextWrapping.Wrap };
        AutomationProperties.SetAutomationId(status, "fixture-status");
        panel.Children.Add(status);
        var counter = new TextBlock { Text = "Counter: 0", FontSize = 14, Margin = new Thickness(0, 10, 0, 0) };
        AutomationProperties.SetAutomationId(counter, "fixture-counter");
        panel.Children.Add(counter);
        if (args.Contains("--many-nodes"))
        {
            for (int i = 0; i < 350; i++)
            {
                var item = new Button { Content = "Discovery fixture item " + i };
                AutomationProperties.SetAutomationId(item, "discovery-item-" + i);
                panel.Children.Add(item);
            }
        }
        var applied = 0;
        apply.Click += (_, _) =>
        {
            applied++;
            status.Text = $"Applied {applied}: {input.Text}";
            counter.Text = $"Counter: {applied}";
            Console.WriteLine(JsonSerializer.Serialize(new { @event = "applied", count = applied, value = input.Text }));
            Console.Out.Flush();
        };
        fixture.Content = panel;
        fixture.ContentRendered += (_, _) =>
        {
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                @event = "ready",
                fixturePID = Environment.ProcessId,
                hwnd = new WindowInteropHelper(fixture).Handle.ToInt64()
            }));
            Console.Out.Flush();
        };
        var commands = new Thread(() =>
        {
            while (Console.ReadLine() is { } command)
            {
                if (command == "stop")
                {
                    app.Dispatcher.BeginInvoke(() => fixture.Close());
                    return;
                }
            }
            app.Dispatcher.BeginInvoke(() => fixture.Close());
        }) { IsBackground = true };
        commands.Start();
        app.Run(fixture);
    }
}
