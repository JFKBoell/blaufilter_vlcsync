--[[ Blaufilter control interface for VLC 3.

Why this exists: VLC's RC interface reports the playback position in whole
seconds (cli.lua does `math.floor(vlc.var.get(input,"time") / 1000000)`) and
only seeks to whole seconds, and its `pause` is a toggle. The microsecond value
is right there in Lua — this script hands it out unrounded, seeks with the same
precision, and offers an explicit play/pause that cannot flip the wrong way.

Install as ~/.local/share/vlc/lua/intf/blaufilter.lua and start VLC with

    cvlc -I luaintf --lua-intf blaufilter \
         --lua-config "blaufilter={host='0.0.0.0:4214'}" ...

Protocol: one request per line, one reply per line, ASCII, no prompt.

    t                -> t <time_us> <length_us> <rate> <state> <mdate_us>
    s <time_us>      -> ok            (absolute seek, microseconds)
    r <rate>         -> ok
    play | pause     -> ok            (explicit, idempotent — not a toggle)
    ping             -> pong <mdate_us>
    <anything else>  -> err

`mdate_us` is this device's own monotonic clock at the moment `time` was read.
It lets the controller date a reading on the device's clock instead of guessing
from the round trip, which is where most of the measurement noise came from.

Note on `time`: VLC refreshes it whenever the demuxer's clock has advanced by
about 250 ms, so it steps rather than flows — but each step carries the exact
position in microseconds. The controller detects the step and dates it, so the
accuracy is the poll interval, not the step size. The value tracks the demuxer,
which runs ahead of the picture by the output buffer; that lead is constant per
device and is handled by the offset calibration, not here.
]]

local HOST = "0.0.0.0"
local PORT = 4214

do
    local configured = (type(config) == "table") and config.host or nil
    if configured then
        local host, port = string.match(configured, "^(.*):(%d+)$")
        if port then
            HOST, PORT = host, tonumber(port)
        else
            PORT = tonumber(configured) or PORT
        end
    end
    if HOST == "" then HOST = "0.0.0.0" end
end

-- VLC's input state enum: 0 INIT, 1 OPENING, 2 PLAYING, 3 PAUSE, 4 END, 5 ERROR
local STATE_PLAYING, STATE_PAUSED = 2, 3
local STATE_NAMES = {
    [0] = "opening", [1] = "opening", [2] = "playing",
    [3] = "paused", [4] = "stopped", [5] = "error",
}

local function us(value)
    -- %.0f, not %d: in Lua 5.3 %d rejects a number without an integer
    -- representation, and these come from C as plain numbers.
    return string.format("%.0f", value or 0)
end

local function status_line()
    local input = vlc.object.input()
    if not input then
        return "t 0 0 0 none " .. us(vlc.misc.mdate())
    end
    -- Read the clock as close to the position as possible
    local time = vlc.var.get(input, "time")
    local at = vlc.misc.mdate()
    local length = vlc.var.get(input, "length")
    local rate = vlc.var.get(input, "rate")
    local state = STATE_NAMES[vlc.var.get(input, "state")] or "unknown"
    return table.concat({
        "t", us(time), us(length), string.format("%.4f", rate or 1.0),
        state, us(at),
    }, " ")
end

local function handle(line)
    local cmd, arg = string.match(line, "^%s*(%S+)%s*(.-)%s*$")
    if not cmd then return "err empty" end

    if cmd == "t" then
        return status_line()
    elseif cmd == "ping" then
        return "pong " .. us(vlc.misc.mdate())
    end

    local input = vlc.object.input()
    if not input then return "err no input" end

    if cmd == "s" then
        local target = tonumber(arg)
        if not target then return "err bad seek" end
        if target < 0 then target = 0 end
        vlc.var.set(input, "time", target)
        return "ok"
    elseif cmd == "r" then
        local rate = tonumber(arg)
        if not rate or rate <= 0 then return "err bad rate" end
        vlc.var.set(input, "rate", rate)
        return "ok"
    elseif cmd == "play" then
        vlc.var.set(input, "state", STATE_PLAYING)
        return "ok"
    elseif cmd == "pause" then
        vlc.var.set(input, "state", STATE_PAUSED)
        return "ok"
    end
    return "err unknown"
end

local listener = vlc.net.listen_tcp(HOST, PORT)
vlc.msg.info("[blaufilter] listening on " .. HOST .. ":" .. tostring(PORT))

local listen_fds = {}
for _, fd in ipairs({ listener:fds() }) do listen_fds[fd] = true end

local buffers = {}      -- fd -> partial line
local pollfds = {}

local function drop(fd)
    buffers[fd] = nil
    vlc.net.close(fd)
end

while true do
    for fd in pairs(pollfds) do pollfds[fd] = nil end
    for fd in pairs(listen_fds) do pollfds[fd] = vlc.net.POLLIN end
    for fd in pairs(buffers) do pollfds[fd] = vlc.net.POLLIN end

    -- Blocks until a client says something, or until VLC shuts down, which
    -- raises "Interrupted." — that is the only way out of this loop (VLC 3 has
    -- no vlc.misc.should_die()).
    local alive = pcall(vlc.net.poll, pollfds)
    if not alive then break end

    -- accept() blocks when nothing is pending, so only ask when poll said so
    for fd in pairs(listen_fds) do
        if pollfds[fd] and pollfds[fd] ~= 0 then
            local accepted = listener:accept()
            if accepted and accepted >= 0 then buffers[accepted] = "" end
        end
    end

    for fd, events in pairs(pollfds) do
        if buffers[fd] and events and events ~= 0 then
            local chunk = vlc.net.recv(fd, 4096)
            if not chunk or chunk == "" then
                drop(fd)
            else
                local buffer = buffers[fd] .. chunk
                if #buffer > 8192 then buffer = "" end   -- no line in sight
                while true do
                    local line, rest = string.match(buffer, "^(.-)\r?\n(.*)$")
                    if not line then break end
                    buffer = rest
                    local ok, reply = pcall(handle, line)
                    if not ok then reply = "err internal" end
                    if not vlc.net.send(fd, reply .. "\n") then
                        buffer = nil
                        break
                    end
                end
                if buffer == nil then drop(fd) else buffers[fd] = buffer end
            end
        end
    end
end

for fd in pairs(buffers) do vlc.net.close(fd) end
