using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Shell;
using System.Windows.Threading;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Windows.SDK.BuildTools.WinApp.UIAutomation;
using Windows.Graphics.Capture;
using Windows.Security.Authorization.AppCapabilityAccess;

namespace BackgroundControl;

public sealed record CaptureSnapshot(
    bool Active, bool Paused, bool IsClosed, int Width, int Height,
    long FrameVersion, long AgeMs, string? PngBase64, string Status);

/// <summary>
/// Direct, window-scoped WGC capture and a plugin-owned preview. UI operations belong to the
/// supplied dispatcher; Snapshot can be called from another thread. There is no screen-capture,
/// PrintWindow, input-injection, activation, or target-restoration fallback.
/// </summary>
public sealed class CapturePreview : IDisposable
{
    private readonly Dispatcher _dispatcher;
    private readonly Action<string> _onCommand;
    private readonly ServiceProvider _services;
    private readonly IWindowCapture _capture;
    private readonly DispatcherTimer _timer;
    private readonly object _snapshotLock = new();
    private IFrameGrabber? _grabber;
    private Window? _window;
    private Image? _image;
    private TextBlock? _statusText;
    private TextBlock? _emptyText;
    private Button? _pauseButton;
    private Grid? _surface;
    private Grid? _chrome;
    private Border? _stateBadge;
    private TextBlock? _stateBadgeText;
    private TextBlock? _pauseLabel;
    private System.Windows.Shapes.Path? _pauseIcon;
    private System.Windows.Shapes.Path? _virtualPointer;
    private double? _pointerX, _pointerY;
    private bool _chromeHovered;
    private double _frameAspect = 16.0 / 9;
    private bool _userSizing;
    private Task<AppCapabilityAccessStatus>? _borderlessAccess;
    private string _borderlessNotice = "";
    private nint _hwnd;
    private uint _targetPid;
    private uint _targetThread;
    private bool _disposed;
    private bool _internalClose;
    private bool _unavailable;
    private bool _wasHidden;
    private bool _resizePending;
    private long _resizeObservedAt;
    private long _grabberStartedAt;
    private long _grabberVersion = -1;
    private int _observedWidth;
    private int _observedHeight;
    private string _hostStatus = "";
    private string _captureStatus = "Stopped";

    // Shared snapshot state. The frozen bitmap is immutable and can be encoded outside the UI thread.
    private bool _active;
    private bool _paused;
    private bool _isClosed;
    private int _width;
    private int _height;
    private long _frameVersion;
    private long _lastFrameAt;
    private string _status = "Stopped";
    private BitmapSource? _latestBitmap;

    public CapturePreview(Dispatcher dispatcher, Action<string> onCommand)
    {
        _dispatcher = dispatcher ?? throw new ArgumentNullException(nameof(dispatcher));
        _onCommand = onCommand ?? throw new ArgumentNullException(nameof(onCommand));
        _dispatcher.VerifyAccess();
        _services = new ServiceCollection()
            .AddSingleton(typeof(ILogger<>), typeof(NullLogger<>))
            .AddWinAppUiAutomation()
            .BuildServiceProvider();
        // Input services registered by this extension are never resolved or invoked here.
        _capture = _services.GetRequiredService<IWindowCapture>();
        if (_capture.GetType().FullName != "Microsoft.Windows.SDK.BuildTools.WinApp.UIAutomation.WgcWindowCapture")
        {
            _services.Dispose();
            throw new PlatformNotSupportedException("The preview requires the direct Windows Graphics Capture implementation.");
        }
        _timer = new DispatcherTimer(DispatcherPriority.Background, _dispatcher)
        {
            Interval = TimeSpan.FromMilliseconds(50)
        };
        _timer.Tick += OnTick;
    }

    public void Start(nint hwnd, string title)
    {
        VerifyUsable();
        if (hwnd == nint.Zero || !Native.IsWindow(hwnd))
            throw new ArgumentException("The selected window no longer exists.", nameof(hwnd));
        var thread = Native.GetWindowThreadProcessId(hwnd, out var pid);
        if (thread == 0 || pid == 0)
            throw new ArgumentException("The selected window's identity could not be verified.", nameof(hwnd));

        Stop();
        _hwnd = hwnd;
        _targetPid = pid;
        _targetThread = thread;
        _unavailable = false;
        _wasHidden = false;
        _resizePending = false;
        _userSizing = false;
        _frameAspect = Native.GetWindowRect(hwnd, out var initialBounds) && initialBounds.Width > 0 && initialBounds.Height > 0
            ? (double)initialBounds.Width / initialBounds.Height : 16.0 / 9;
        _hostStatus = "";
        lock (_snapshotLock)
        {
            _active = true;
            _paused = false;
            _isClosed = false;
            _width = _height = 0;
            _frameVersion = 0;
            _lastFrameAt = 0;
            _latestBitmap = null;
        }
        _captureStatus = "Starting capture";
        CreateWindow(string.IsNullOrWhiteSpace(title) ? "Selected window" : title);
        _timer.Start();
        UpdateCapture();
    }

    // WGC is change-driven: a perfectly static window may produce no new frames.
    // When input needs current pixels, request a new window-scoped capture session
    // rather than assigning a new timestamp to an old bitmap. Keep the displayed
    // bitmap while waiting, but do not return it as a fresh input observation.
    public async Task<CaptureSnapshot> SnapshotForInputAsync(bool includeScreenshot)
    {
        var request = _dispatcher.Invoke(() =>
        {
            var current = Snapshot(false);
            if (!current.Active || current.IsClosed || current.AgeMs is >= 0 and <= 750)
                return (Refresh: false, Hwnd: _hwnd, Version: current.FrameVersion);
            if (!_unavailable && !_wasHidden && !_resizePending && Native.IsWindow(_hwnd) && !Native.IsIconic(_hwnd))
            {
                DisposeGrabber();
                StartGrabber();
            }
            return (Refresh: true, Hwnd: _hwnd, Version: current.FrameVersion);
        });
        var watch = Stopwatch.StartNew();
        while (request.Refresh && watch.ElapsedMilliseconds < 1500)
        {
            await Task.Delay(25).ConfigureAwait(false);
            if (_dispatcher.HasShutdownStarted || _dispatcher.HasShutdownFinished)
                throw new InvalidOperationException("capture_cancelled");
            var ready = _dispatcher.Invoke(() =>
            {
                if (_hwnd != request.Hwnd || !_active) throw new InvalidOperationException("capture_target_changed");
                return _frameVersion > request.Version && _lastFrameAt != 0 && ElapsedMs(_lastFrameAt) <= 750;
            });
            if (ready) break;
        }
        return _dispatcher.Invoke(() => Snapshot(includeScreenshot));
    }

    public void Stop()
    {
        _dispatcher.VerifyAccess();
        if (_disposed) return;
        _timer.Stop();
        DisposeGrabber();
        lock (_snapshotLock)
        {
            _active = false;
            _paused = false;
            _latestBitmap = null;
            _status = _isClosed ? "Selected window closed" : "Stopped";
        }
        _hwnd = nint.Zero;
        _hostStatus = "";
        _captureStatus = _isClosed ? "Selected window closed" : "Stopped";
        ClosePreviewWindow();
    }

    /// <summary>Records the host's automation pause state. The app's actual live preview continues.</summary>
    public void SetPaused(bool paused)
    {
        VerifyUsable();
        lock (_snapshotLock) _paused = paused;
        RefreshStatus();
    }

    public void SetStatus(string status)
    {
        VerifyUsable();
        _hostStatus = status ?? "";
        RefreshStatus();
    }

    public CaptureSnapshot Snapshot(bool includeImage)
    {
        CaptureSnapshot result;
        BitmapSource? bitmap;
        lock (_snapshotLock)
        {
            bitmap = _active && !_isClosed ? _latestBitmap : null;
            result = new CaptureSnapshot(_active, _paused, _isClosed, _width, _height,
                _frameVersion, _lastFrameAt == 0 ? -1 : ElapsedMs(_lastFrameAt), null, _status);
        }
        if (includeImage && bitmap is not null)
        {
            using var stream = new MemoryStream();
            var encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(bitmap));
            encoder.Save(stream);
            result = result with { PngBase64 = Convert.ToBase64String(stream.ToArray()) };
        }
        return result;
    }

    // Normalized source-frame position comes only from acknowledged virtual-input state.
    // This overlay has no physical cursor polling and cannot create input by itself.
    public void SetVirtualPointer(double? x, double? y)
    {
        _dispatcher.VerifyAccess();
        _pointerX = x; _pointerY = y;
        UpdateVirtualPointer();
    }
    private void UpdateVirtualPointer()
    {
        if (_virtualPointer is null || _surface is null) return;
        bool visible = _pointerX is >= 0 and <= 1 && _pointerY is >= 0 and <= 1;
        _virtualPointer.Visibility = visible ? Visibility.Visible : Visibility.Collapsed;
        if (!visible) return;
        double width = _surface.ActualWidth, height = _surface.ActualHeight;
        double contentWidth = Math.Min(width, height * _frameAspect), contentHeight = contentWidth / _frameAspect;
        _virtualPointer.RenderTransform = new TranslateTransform((width - contentWidth) / 2 + _pointerX!.Value * contentWidth,
            (height - contentHeight) / 2 + _pointerY!.Value * contentHeight);
    }

    private void OnTick(object? sender, EventArgs e)
    {
        if (_disposed || !_active) return;
        try { UpdateCapture(); }
        catch (Exception ex)
        {
            DisposeGrabber();
            ClearImage();
            _unavailable = true;
            _captureStatus = "Capture unavailable: " + ex.Message;
            RefreshStatus();
        }
    }

    private void UpdateCapture()
    {
        var thread = Native.GetWindowThreadProcessId(_hwnd, out var pid);
        if (!Native.IsWindow(_hwnd) || pid != _targetPid || thread != _targetThread || _grabber?.IsClosed == true)
        {
            TargetClosed();
            return;
        }
        var minimized = Native.IsIconic(_hwnd);
        var hidden = !Native.IsWindowVisible(_hwnd);
        if (minimized || hidden)
        {
            DisposeGrabber();
            ClearImage();
            _wasHidden = true;
            _captureStatus = minimized ? "Selected window is minimized — capture unavailable" : "Selected window is hidden — capture unavailable";
            RefreshStatus();
            return;
        }
        if (_wasHidden)
        {
            _wasHidden = false;
            _unavailable = false;
            _resizePending = false;
        }
        if (!Native.GetWindowRect(_hwnd, out var bounds) || bounds.Width <= 0 || bounds.Height <= 0)
        {
            _captureStatus = "Selected window has no captureable area";
            ClearImage();
            RefreshStatus();
            return;
        }
        if (_unavailable) { RefreshStatus(); return; }
        if (_grabber is null)
        {
            _observedWidth = bounds.Width;
            _observedHeight = bounds.Height;
            StartGrabber();
        }
        else if (bounds.Width != _observedWidth || bounds.Height != _observedHeight)
        {
            _observedWidth = bounds.Width;
            _observedHeight = bounds.Height;
            _resizeObservedAt = Stopwatch.GetTimestamp();
            _resizePending = true;
            _captureStatus = "Window resized — refreshing capture";
        }
        if (_resizePending && ElapsedMs(_resizeObservedAt) >= 150)
        {
            // The library may discard the frame that triggered pool recreation and wait for a
            // further dirty frame. A new window-scoped session obtains the current static surface.
            DisposeGrabber();
            ClearImage();
            _resizePending = false;
            StartGrabber();
        }
        if (_grabber?.TryGetLatest() is { } frame && frame.Version != _grabberVersion)
        {
            var requiredBytes = checked(frame.Width * frame.Height * 4);
            if (frame.Width <= 0 || frame.Height <= 0 || frame.Pixels.Length < requiredBytes)
                throw new InvalidOperationException("WGC returned invalid frame dimensions.");
            var bitmap = BitmapSource.Create(frame.Width, frame.Height, 96, 96, PixelFormats.Bgra32,
                null, frame.Pixels, checked(frame.Width * 4));
            bitmap.Freeze();
            _grabberVersion = frame.Version;
            lock (_snapshotLock)
            {
                _latestBitmap = bitmap;
                _width = frame.Width;
                _height = frame.Height;
                _frameVersion++;
                _lastFrameAt = Stopwatch.GetTimestamp();
            }
            if (_image is not null) _image.Source = bitmap;
            var aspect = (double)frame.Width / frame.Height;
            if (Math.Abs(_frameAspect - aspect) > 0.000001)
            {
                _frameAspect = aspect;
                if (!_userSizing) FitPreviewToAspect();
            }
            if (_emptyText is not null) _emptyText.Visibility = Visibility.Collapsed;
            if (!_resizePending) _captureStatus = "Live preview";
        }
        else if (_latestBitmap is null && _grabber is not null && ElapsedMs(_grabberStartedAt) >= 3000)
        {
            _captureStatus = "No window frames available";
        }
        RefreshStatus();
    }

    private void StartGrabber()
    {
        if (!_capture.IsFrameCaptureSupported)
            throw new PlatformNotSupportedException("Windows Graphics Capture is not available in this session.");
        if (OperatingSystem.IsWindowsVersionAtLeast(10, 0, 20348))
        {
            _borderlessAccess ??= RequestBorderlessAccess();
            if (!_borderlessAccess.IsCompleted)
            {
                _captureStatus = "Waiting for Windows capture permission";
                return;
            }
        }
        _grabber = _capture.StartFrameGrabber(_hwnd, fps: 15);
        ConfigureCaptureBorder(_grabber);
        _grabberStartedAt = Stopwatch.GetTimestamp();
        _grabberVersion = -1;
        _captureStatus = "Waiting for window frames";
    }

    [System.Runtime.Versioning.SupportedOSPlatform("windows10.0.20348")]
    private static async Task<AppCapabilityAccessStatus> RequestBorderlessAccess() =>
        await GraphicsCaptureAccess.RequestAccessAsync(GraphicsCaptureAccessKind.Borderless);

    private void ConfigureCaptureBorder(IFrameGrabber grabber)
    {
        _borderlessNotice = "";
        try
        {
            if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 20348))
                throw new NotSupportedException("Windows does not support borderless capture");
            if (_borderlessAccess?.GetAwaiter().GetResult() != AppCapabilityAccessStatus.Allowed)
                throw new InvalidOperationException("Windows has not allowed borderless capture");
            // WinApp.UIAutomation 0.7.0 has no public session option. Keep this adapter
            // scoped to its pinned grabber; use the normal Windows consent/property API.
            var type = grabber.GetType();
            if (type.FullName != "Microsoft.Windows.SDK.BuildTools.WinApp.UIAutomation.WgcCapture+FrameGrabber" ||
                type.GetField("_session", BindingFlags.Instance | BindingFlags.NonPublic)?.GetValue(grabber)
                    is not GraphicsCaptureSession session)
                throw new NotSupportedException("The capture library's session adapter needs updating");
            session.IsBorderRequired = false;
        }
        catch (Exception ex)
        {
            _borderlessNotice = "Capture outline remains: " + ex.Message;
        }
    }

    private void TargetClosed()
    {
        lock (_snapshotLock) _isClosed = true;
        Stop();
        _onCommand("stop");
    }

    private void ClearImage()
    {
        lock (_snapshotLock)
        {
            _latestBitmap = null;
            _width = _height = 0;
            _lastFrameAt = 0;
        }
        if (_image is not null) _image.Source = null;
        if (_emptyText is not null) _emptyText.Visibility = Visibility.Visible;
    }

    private void RefreshStatus()
    {
        string status;
        bool paused;
        lock (_snapshotLock)
        {
            paused = _paused;
            status = (paused ? "Paused · " : "") + _captureStatus;
            if (!string.IsNullOrWhiteSpace(_hostStatus)) status += " · " + _hostStatus;
            if (!string.IsNullOrWhiteSpace(_borderlessNotice)) status += " · " + _borderlessNotice;
            _status = status;
        }
        if (_statusText is not null) _statusText.Text = status;
        if (_emptyText is not null) _emptyText.Text = _captureStatus;
        if (_pauseLabel is not null && _pauseLabel.Text != (paused ? "Resume" : "Pause"))
        {
            _pauseLabel.Text = paused ? "Resume" : "Pause";
            if (_pauseIcon is not null) _pauseIcon.Data = Geometry.Parse(paused
                ? "M5,2 L18,10 5,18 Z" : "M4,2 H8 V18 H4 Z M12,2 H16 V18 H12 Z");
        }
        if (_pauseButton is not null)
        {
            _pauseButton.ToolTip = paused ? "Resume background control" : "Pause background control";
            System.Windows.Automation.AutomationProperties.SetName(_pauseButton, paused ? "Resume" : "Pause");
        }
        RefreshStateBadge();
    }

    private void CreateWindow(string title)
    {
        var work = SystemParameters.WorkArea;
        var initialWidth = Math.Min(520, Math.Min(Math.Max(1, work.Width - 40), Math.Max(1, work.Height - 40) * _frameAspect));
        var initialHeight = initialWidth / _frameAspect;
        var window = new Window
        {
            Title = "Background control — " + title, Width = initialWidth, Height = initialHeight,
            Left = Math.Max(work.Left, work.Right - initialWidth - 20), Top = Math.Max(work.Top, work.Bottom - initialHeight - 20),
            Topmost = true, ShowActivated = false, ShowInTaskbar = false,
            WindowStartupLocation = WindowStartupLocation.Manual,
            WindowStyle = WindowStyle.None, ResizeMode = ResizeMode.CanResize, MinWidth = 1, MinHeight = 1,
            Background = Brushes.Black, Foreground = Brushes.White, Focusable = false,
            UseLayoutRounding = true, SnapsToDevicePixels = true
        };
        WindowChrome.SetWindowChrome(window, new WindowChrome
        {
            CaptionHeight = 0, ResizeBorderThickness = new Thickness(6),
            GlassFrameThickness = new Thickness(0), CornerRadius = new CornerRadius(8), UseAeroCaptionButtons = false
        });
        _window = window;
        _chromeHovered = false;
        window.SourceInitialized += (_, _) =>
        {
            var hwnd = new WindowInteropHelper(window).Handle;
            var style = Native.GetWindowLongPtr(hwnd, -20).ToInt64();
            Native.SetWindowLongPtr(hwnd, -20, new nint(style | 0x08000080L));
            if ((Native.GetWindowLongPtr(hwnd, -20).ToInt64() & 0x08000000L) == 0)
                throw new Win32Exception("Could not configure the preview to avoid activation.");
            HwndSource.FromHwnd(hwnd)?.AddHook((nint handle, int message, nint wp, nint lp, ref bool handled) =>
            {
                if (message == 0x0021) { handled = true; return new nint(3); } // WM_MOUSEACTIVATE / MA_NOACTIVATE
                if (message == 0x0231) _userSizing = true; // WM_ENTERSIZEMOVE
                if (message == 0x0214 && lp != nint.Zero) // WM_SIZING, native-pixel outer rectangle
                {
                    var proposed = Marshal.PtrToStructure<Native.Rect>(lp);
                    if (ConstrainPreviewResize(handle, wp.ToInt32(), ref proposed))
                    {
                        Marshal.StructureToPtr(proposed, lp, false);
                        handled = true;
                        return new nint(1);
                    }
                }
                if (message == 0x0232) // WM_EXITSIZEMOVE
                {
                    _userSizing = false;
                    _dispatcher.BeginInvoke(() => FitPreviewToAspect());
                }
                if (message == 0x02E0) // WM_DPICHANGED: let WPF apply monitor DPI before fitting.
                    _dispatcher.BeginInvoke(() => FitPreviewToAspect());
                return nint.Zero;
            });
        };
        var surface = _surface = new Grid { Background = Brushes.Black, ClipToBounds = true };
        _image = new Image { Stretch = Stretch.Uniform };
        _emptyText = new TextBlock { Text = _captureStatus, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(15),
            HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, Foreground = Brushes.LightGray };
        surface.Children.Add(_image); surface.Children.Add(_emptyText);
        _pointerX = _pointerY = null;
        _virtualPointer = new System.Windows.Shapes.Path { Data = Geometry.Parse("M 0,0 L 0,17 L 4,13 L 8,21 L 11,19 L 7,12 L 14,12 Z"),
            Fill = Brushes.White, Stroke = Brushes.Black, StrokeThickness = 1.5, IsHitTestVisible = false,
            HorizontalAlignment = HorizontalAlignment.Left, VerticalAlignment = VerticalAlignment.Top, Visibility = Visibility.Collapsed };
        surface.Children.Add(_virtualPointer);
        surface.SizeChanged += (_, _) => UpdateVirtualPointer();

        var chrome = _chrome = new Grid { Opacity = 0, IsHitTestVisible = false };
        chrome.Children.Add(new Border { Height = 78, VerticalAlignment = VerticalAlignment.Top,
            Background = FadeBrush(0.76, false), IsHitTestVisible = false });
        chrome.Children.Add(new Border { Height = 82, VerticalAlignment = VerticalAlignment.Bottom,
            Background = FadeBrush(0.76, true), IsHitTestVisible = false });

        var header = new Grid { VerticalAlignment = VerticalAlignment.Top, Margin = new Thickness(14, 10, 10, 0) };
        header.ColumnDefinitions.Add(new ColumnDefinition());
        header.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var titleText = new TextBlock { Text = title, FontFamily = new FontFamily("Segoe UI"), FontSize = 12,
            FontWeight = FontWeights.Medium, TextTrimming = TextTrimming.CharacterEllipsis,
            VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 14, 0), IsHitTestVisible = false };
        header.Children.Add(titleText);
        var stop = OverlayButton("preview.stop", "Stop control and close preview", 32);
        stop.Content = new System.Windows.Shapes.Path { Data = Geometry.Parse("M4,4 L14,14 M14,4 L4,14"),
            Stroke = Brushes.White, StrokeThickness = 1.6, Width = 14, Height = 14, Stretch = Stretch.Uniform,
            StrokeStartLineCap = PenLineCap.Round, StrokeEndLineCap = PenLineCap.Round };
        System.Windows.Automation.AutomationProperties.SetName(stop, "Stop control and close preview");
        stop.Click += (_, _) => { Stop(); _onCommand("stop"); };
        Grid.SetColumn(stop, 1); header.Children.Add(stop); chrome.Children.Add(header);

        _pauseButton = OverlayButton("preview.pause", "Pause background control", 38);
        _pauseButton.Padding = new Thickness(16, 0, 16, 0);
        _pauseButton.HorizontalAlignment = HorizontalAlignment.Center;
        _pauseButton.VerticalAlignment = VerticalAlignment.Bottom;
        _pauseButton.Margin = new Thickness(0, 0, 0, 14);
        var pauseContent = new StackPanel { Orientation = Orientation.Horizontal };
        _pauseIcon = new System.Windows.Shapes.Path { Data = Geometry.Parse("M4,2 H8 V18 H4 Z M12,2 H16 V18 H12 Z"),
            Fill = Brushes.White, Width = 13, Height = 15, Stretch = Stretch.Uniform, VerticalAlignment = VerticalAlignment.Center };
        _pauseLabel = new TextBlock { Text = "Pause", FontFamily = new FontFamily("Segoe UI"), FontSize = 12,
            FontWeight = FontWeights.SemiBold, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(9, 0, 0, 0) };
        pauseContent.Children.Add(_pauseIcon); pauseContent.Children.Add(_pauseLabel); _pauseButton.Content = pauseContent;
        _pauseButton.Click += (_, _) => _onCommand(_paused ? "resume" : "pause");
        chrome.Children.Add(_pauseButton);
        _statusText = new TextBlock { Text = _captureStatus, FontFamily = new FontFamily("Segoe UI"),
            FontSize = 11, Foreground = new SolidColorBrush(Color.FromRgb(215, 218, 224)),
            TextTrimming = TextTrimming.CharacterEllipsis, VerticalAlignment = VerticalAlignment.Bottom,
            HorizontalAlignment = HorizontalAlignment.Left, Margin = new Thickness(14, 0, 0, 26),
            MaxWidth = 130, IsHitTestVisible = false };
        // At the smallest size, reserve the center for the action and omit redundant live text.
        surface.SizeChanged += (_, _) =>
        {
            if (_statusText is not null) _statusText.MaxWidth = Math.Max(0, (surface.ActualWidth - 190) / 2);
        };
        chrome.Children.Add(_statusText);
        surface.Children.Add(chrome);
        _stateBadgeText = new TextBlock { FontFamily = new FontFamily("Segoe UI"), FontSize = 11, Foreground = Brushes.White };
        _stateBadge = new Border { Child = _stateBadgeText, CornerRadius = new CornerRadius(5),
            Background = new SolidColorBrush(Color.FromArgb(195, 18, 20, 25)), Padding = new Thickness(8, 5, 8, 5),
            Margin = new Thickness(12), HorizontalAlignment = HorizontalAlignment.Left,
            VerticalAlignment = VerticalAlignment.Bottom, Visibility = Visibility.Collapsed, IsHitTestVisible = false };
        surface.Children.Add(_stateBadge);
        surface.MouseEnter += (_, _) => SetChromeVisible(true);
        surface.MouseLeave += (_, _) => SetChromeVisible(false);
        surface.MouseLeftButtonDown += (_, e) =>
        {
            if (e.ChangedButton != MouseButton.Left || e.ButtonState != MouseButtonState.Pressed) return;
            for (var node = e.OriginalSource as DependencyObject; node is not null;
                node = node is Visual ? VisualTreeHelper.GetParent(node) : LogicalTreeHelper.GetParent(node))
                if (node is Button) return;
            // Only a real user press initiates native window dragging. Capture pixels are not clicked through.
            try { window.DragMove(); SetChromeVisible(surface.IsMouseOver); e.Handled = true; }
            catch (InvalidOperationException) { }
        };
        window.Content = surface;
        window.Closed += (_, _) =>
        {
            _window = null; _image = null; _emptyText = null; _statusText = null; _pauseButton = null;
            _surface = null; _chrome = null; _stateBadge = null; _stateBadgeText = null; _pauseLabel = null; _pauseIcon = null;
            if (!_internalClose) { Stop(); _onCommand("stop"); }
        };
        RefreshStatus();
        // Native edge resizing and user-initiated dragging remain available, without an OS title bar.
        window.Show();
        FitPreviewToAspect();
        SetChromeVisible(surface.IsMouseOver);
    }

    // Resize the preview frame itself to the capture ratio. Uniform image scaling
    // remains a safeguard; cropping/stretching the captured app is not the fix.
    private void FitPreviewToAspect()
    {
        if (_window is null || _userSizing) return;
        var handle = new WindowInteropHelper(_window).Handle;
        if (!TryResizeMetrics(handle, out var metrics)) return;
        _window.MinWidth = (metrics.MinWidth + metrics.ExtraWidth) / metrics.Scale;
        _window.MinHeight = (Math.Round(metrics.MinWidth / _frameAspect) + metrics.ExtraHeight) / metrics.Scale;
        var width = Math.Clamp(metrics.Current.Width - metrics.ExtraWidth, metrics.MinWidth, metrics.MaxWidth);
        var height = (int)Math.Round(width / _frameAspect) + metrics.ExtraHeight;
        width += metrics.ExtraWidth;
        var left = Math.Clamp(metrics.Current.Left, metrics.Work.Left, Math.Max(metrics.Work.Left, metrics.Work.Right - width));
        var top = Math.Clamp(metrics.Current.Top, metrics.Work.Top, Math.Max(metrics.Work.Top, metrics.Work.Bottom - height));
        Native.SetWindowPos(handle, nint.Zero, left, top, width, height, 0x0004 | 0x0010 | 0x0200);
    }

    private bool ConstrainPreviewResize(nint handle, int edge, ref Native.Rect proposed)
    {
        if (edge < 1 || edge > 8 || !TryResizeMetrics(handle, out var metrics)) return false;
        var width = Math.Max(1, proposed.Width - metrics.ExtraWidth);
        var height = Math.Max(1, proposed.Height - metrics.ExtraHeight);
        var currentWidth = Math.Max(1, metrics.Current.Width - metrics.ExtraWidth);
        var currentHeight = Math.Max(1, metrics.Current.Height - metrics.ExtraHeight);
        bool widthDriven = edge is 1 or 2 || (edge is 4 or 5 or 7 or 8 &&
            Math.Abs(width - currentWidth) >= Math.Abs(height - currentHeight) * _frameAspect);
        var desiredWidth = widthDriven ? width : height * _frameAspect;
        width = (int)Math.Round(Math.Clamp(desiredWidth, metrics.MinWidth, metrics.MaxWidth));
        height = (int)Math.Round(width / _frameAspect) + metrics.ExtraHeight;
        width += metrics.ExtraWidth;
        // Side drags keep the opposite edge plus top/left. Corners keep their opposite corner.
        if (edge is 1 or 4 or 7) proposed.Left = proposed.Right - width;
        else proposed.Right = proposed.Left + width;
        if (edge is 3 or 4 or 5) proposed.Top = proposed.Bottom - height;
        else proposed.Bottom = proposed.Top + height;
        return true;
    }

    private bool TryResizeMetrics(nint handle, out ResizeMetrics metrics)
    {
        metrics = default;
        if (handle == nint.Zero || !Native.GetWindowRect(handle, out var current) ||
            !Native.GetClientRect(handle, out var client) || current.Width <= 0 || current.Height <= 0) return false;
        var extraWidth = Math.Max(0, current.Width - client.Width);
        var extraHeight = Math.Max(0, current.Height - client.Height);
        var info = new Native.MonitorInfo { Size = Marshal.SizeOf<Native.MonitorInfo>() };
        var work = Native.GetMonitorInfo(Native.MonitorFromWindow(handle, 2), ref info) ? info.Work : current;
        var dpi = Native.GetDpiForWindow(handle);
        var scale = dpi == 0 ? 1.0 : dpi / 96.0;
        var maxWidth = Math.Max(1, (int)Math.Floor(Math.Min(Math.Max(1, work.Width - extraWidth),
            Math.Max(1, work.Height - extraHeight) * _frameAspect)));
        var minWidth = Math.Min(maxWidth, Math.Max(1, (int)Math.Ceiling(Math.Max(
            280 * scale - extraWidth, (180 * scale - extraHeight) * _frameAspect))));
        metrics = new(current, work, extraWidth, extraHeight, minWidth, maxWidth, scale);
        return true;
    }

    private readonly record struct ResizeMetrics(Native.Rect Current, Native.Rect Work,
        int ExtraWidth, int ExtraHeight, int MinWidth, int MaxWidth, double Scale);

    private void SetChromeVisible(bool visible)
    {
        _chromeHovered = visible;
        if (_chrome is not null)
        {
            _chrome.IsHitTestVisible = visible;
            _chrome.BeginAnimation(UIElement.OpacityProperty, new DoubleAnimation(visible ? 1 : 0, TimeSpan.FromMilliseconds(120))
            { EasingFunction = new QuadraticEase { EasingMode = EasingMode.EaseOut } });
        }
        RefreshStateBadge();
    }

    private void RefreshStateBadge()
    {
        if (_stateBadge is null || _stateBadgeText is null) return;
        bool attention = !string.IsNullOrWhiteSpace(_hostStatus);
        _stateBadge.Visibility = !_chromeHovered && (_paused || attention) ? Visibility.Visible : Visibility.Collapsed;
        _stateBadgeText.Text = _paused ? "Paused" : "Attention needed";
    }

    private static Brush FadeBrush(double opacity, bool bottom) => new LinearGradientBrush(
        new GradientStopCollection { new(Color.FromArgb((byte)(255 * opacity), 0, 0, 0), 0), new(Color.FromArgb(0, 0, 0, 0), 1) },
        new Point(0, bottom ? 1 : 0), new Point(0, bottom ? 0 : 1));

    private static Button OverlayButton(string id, string tooltip, double height)
    {
        var border = new FrameworkElementFactory(typeof(Border));
        border.Name = "buttonBackground";
        border.SetValue(Border.CornerRadiusProperty, new CornerRadius(8));
        border.SetValue(Border.BackgroundProperty, new SolidColorBrush(Color.FromArgb(145, 35, 37, 43)));
        var presenter = new FrameworkElementFactory(typeof(ContentPresenter));
        presenter.SetValue(ContentPresenter.HorizontalAlignmentProperty, HorizontalAlignment.Center);
        presenter.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
        presenter.SetValue(ContentPresenter.MarginProperty, new TemplateBindingExtension(Control.PaddingProperty));
        border.AppendChild(presenter);
        var template = new ControlTemplate(typeof(Button)) { VisualTree = border };
        var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
        hover.Setters.Add(new Setter(Border.BackgroundProperty, new SolidColorBrush(Color.FromArgb(210, 66, 69, 77)), "buttonBackground"));
        template.Triggers.Add(hover);
        var pressed = new Trigger { Property = System.Windows.Controls.Primitives.ButtonBase.IsPressedProperty, Value = true };
        pressed.Setters.Add(new Setter(Border.BackgroundProperty, new SolidColorBrush(Color.FromArgb(235, 88, 92, 102)), "buttonBackground"));
        template.Triggers.Add(pressed);
        var button = new Button { Template = template, Height = height, MinWidth = height, Focusable = false,
            Foreground = Brushes.White, ToolTip = tooltip, Cursor = Cursors.Arrow };
        System.Windows.Automation.AutomationProperties.SetAutomationId(button, id);
        return button;
    }

    private void DisposeGrabber()
    {
        var grabber = _grabber;
        _grabber = null;
        _grabberVersion = -1;
        grabber?.Dispose();
    }

    private void ClosePreviewWindow()
    {
        if (_window is null) return;
        _internalClose = true;
        try { _window.Close(); }
        finally { _internalClose = false; }
    }

    private void VerifyUsable()
    {
        _dispatcher.VerifyAccess();
        ObjectDisposedException.ThrowIf(_disposed, this);
    }

    public void Dispose()
    {
        _dispatcher.VerifyAccess();
        if (_disposed) return;
        Stop();
        _timer.Tick -= OnTick;
        _services.Dispose();
        _disposed = true;
    }

    private static long ElapsedMs(long timestamp) => (long)Stopwatch.GetElapsedTime(timestamp).TotalMilliseconds;

    private static class Native
    {
        [StructLayout(LayoutKind.Sequential)]
        internal struct Rect
        {
            internal int Left, Top, Right, Bottom;
            internal readonly int Width => Right - Left;
            internal readonly int Height => Bottom - Top;
        }
        [StructLayout(LayoutKind.Sequential)]
        internal struct MonitorInfo
        {
            internal int Size;
            internal Rect Monitor, Work;
            internal uint Flags;
        }
        [DllImport("user32.dll")] internal static extern bool IsWindow(nint hwnd);
        [DllImport("user32.dll")] internal static extern bool IsWindowVisible(nint hwnd);
        [DllImport("user32.dll")] internal static extern bool IsIconic(nint hwnd);
        [DllImport("user32.dll")] internal static extern bool GetWindowRect(nint hwnd, out Rect rect);
        [DllImport("user32.dll")] internal static extern bool GetClientRect(nint hwnd, out Rect rect);
        [DllImport("user32.dll")] internal static extern uint GetDpiForWindow(nint hwnd);
        [DllImport("user32.dll")] internal static extern nint MonitorFromWindow(nint hwnd, uint flags);
        [DllImport("user32.dll", EntryPoint = "GetMonitorInfoW")] internal static extern bool GetMonitorInfo(nint monitor, ref MonitorInfo info);
        [DllImport("user32.dll")] internal static extern bool SetWindowPos(nint hwnd, nint insertAfter, int x, int y, int width, int height, uint flags);
        [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(nint hwnd, out uint processId);
        [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] internal static extern nint GetWindowLongPtr(nint hwnd, int index);
        [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")] internal static extern nint SetWindowLongPtr(nint hwnd, int index, nint value);
    }
}
