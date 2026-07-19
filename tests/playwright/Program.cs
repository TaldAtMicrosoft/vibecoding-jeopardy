using System.Net;
using System.Text;
using Microsoft.Playwright;

// Self-contained Playwright harness for the Vibecoding Jeopardy static site.
// Serves the repo root over HTTP (Playwright can't fetch data/*.csv over file://),
// then drives Chromium to verify the Tips card navigation and ordering.

var repoRoot = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."));
if (!File.Exists(Path.Combine(repoRoot, "index.html")))
{
    Console.Error.WriteLine($"Could not locate index.html from repo root guess: {repoRoot}");
    return 2;
}

// Minimal static file server.
var prefix = "http://localhost:8153/";
var listener = new HttpListener();
listener.Prefixes.Add(prefix);
listener.Start();
var cts = new CancellationTokenSource();
var serverTask = Task.Run(async () =>
{
    while (!cts.IsCancellationRequested)
    {
        HttpListenerContext ctx;
        try { ctx = await listener.GetContextAsync(); }
        catch { break; }
        _ = Task.Run(() =>
        {
            var rel = Uri.UnescapeDataString(ctx.Request.Url!.AbsolutePath.TrimStart('/'));
            if (string.IsNullOrEmpty(rel)) rel = "index.html";
            var full = Path.GetFullPath(Path.Combine(repoRoot, rel));
            if (full.StartsWith(repoRoot) && File.Exists(full))
            {
                var bytes = File.ReadAllBytes(full);
                ctx.Response.ContentType = full.EndsWith(".html") ? "text/html"
                    : full.EndsWith(".js") ? "text/javascript"
                    : full.EndsWith(".css") ? "text/css"
                    : full.EndsWith(".csv") ? "text/csv"
                    : "application/octet-stream";
                ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
            }
            else
            {
                ctx.Response.StatusCode = 404;
            }
            ctx.Response.Close();
        });
    }
});

int failures = 0;
void Check(string name, bool ok, string detail = "")
{
    Console.WriteLine($"  [{(ok ? "PASS" : "FAIL")}] {name}{(detail.Length > 0 ? " -> " + detail : "")}");
    if (!ok) failures++;
}

using (var pw = await Playwright.CreateAsync())
{
    await using var browser = await pw.Chromium.LaunchAsync(new() { Headless = true });
    var page = await browser.NewPageAsync();
    await page.GotoAsync(prefix, new() { WaitUntil = WaitUntilState.NetworkIdle });

    var footer = page.Locator(".tips-card__footer");
    var content = page.Locator(".tips-card__content");
    var next = page.Locator(".tips-card__next");
    var prev = page.Locator(".tips-card__prev");

    await footer.WaitForAsync(new() { State = WaitForSelectorState.Attached });

    // Read the CSV order the app should follow (sorted by index).
    var csv = File.ReadAllText(Path.Combine(repoRoot, "data", "tips.csv"));
    var lines = csv.Replace("\r\n", "\n").Split('\n', StringSplitOptions.RemoveEmptyEntries);
    var expected = new List<string>();
    // header is index,category,points,content -> footer text is "<category> - <points>"
    var records = new List<(int Index, string Footer, string Content)>();
    for (int i = 1; i < lines.Length; i++)
    {
        var fields = ParseCsvLine(lines[i]);
        if (fields.Count < 4 || string.IsNullOrWhiteSpace(fields[3])) continue;
        records.Add((int.Parse(fields[0]), $"{fields[1]} - {fields[2]}", fields[3]));
    }
    records.Sort((a, b) => a.Index.CompareTo(b.Index));

    var firstFooter = (await footer.TextContentAsync() ?? "").Trim();
    Check("first tip is lowest index", firstFooter == records[0].Footer, $"got '{firstFooter}', want '{records[0].Footer}'");

    // Step forward through every tip in index order.
    bool forwardOk = true;
    for (int i = 1; i < records.Count; i++)
    {
        await next.ClickAsync();
        var f = (await footer.TextContentAsync() ?? "").Trim();
        if (f != records[i].Footer) { forwardOk = false; Check($"forward step {i}", false, $"got '{f}', want '{records[i].Footer}'"); break; }
    }
    if (forwardOk) Check("forward steps through all tips in index order", true, $"{records.Count} tips");

    // Next again should wrap to the first.
    await next.ClickAsync();
    var wrap = (await footer.TextContentAsync() ?? "").Trim();
    Check("next wraps to first", wrap == records[0].Footer, $"got '{wrap}'");

    // Back should wrap to the last.
    await prev.ClickAsync();
    var back = (await footer.TextContentAsync() ?? "").Trim();
    Check("back from first wraps to last", back == records[^1].Footer, $"got '{back}'");

    // Prev button must be visible/enabled (the reported bug).
    Check("back button visible", await prev.IsVisibleAsync());
    var opacity = await prev.EvaluateAsync<string>("el => getComputedStyle(el).opacity");
    Check("back button not greyed out", opacity == "1", $"opacity={opacity}");
}

cts.Cancel();
listener.Stop();

Console.WriteLine(failures == 0 ? "\nALL PASSED" : $"\n{failures} CHECK(S) FAILED");
return failures == 0 ? 0 : 1;

static List<string> ParseCsvLine(string line)
{
    var result = new List<string>();
    var sb = new StringBuilder();
    bool inQuotes = false;
    for (int i = 0; i < line.Length; i++)
    {
        char c = line[i];
        if (inQuotes)
        {
            if (c == '"')
            {
                if (i + 1 < line.Length && line[i + 1] == '"') { sb.Append('"'); i++; }
                else inQuotes = false;
            }
            else sb.Append(c);
        }
        else
        {
            if (c == '"') inQuotes = true;
            else if (c == ',') { result.Add(sb.ToString()); sb.Clear(); }
            else sb.Append(c);
        }
    }
    result.Add(sb.ToString());
    return result;
}
