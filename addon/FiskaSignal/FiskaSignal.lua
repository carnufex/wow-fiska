-- FiskaSignal: two 6x6 status squares in the top-left corner of the screen,
-- read by the external fiska.py script.
--   square 1: blue  = always on (marker: the addon is loaded)
--             red   = the player is channeling Fishing (our bobber is in the water)
--             green = a loot window opened during the last 1.5 s (catch confirmed)
--   square 2: blue  = always on (marker)
--             red   = the player is casting something (applying a lure or bait)
--
-- It reads nothing the default UI does not already show and changes nothing.

local FISHING_IDS = {
    [7620] = true, [7731] = true, [7732] = true, [18248] = true, [33095] = true,
    [51294] = true, [88868] = true, [110410] = true, [131474] = true,
    [131476] = true, [131490] = true,
}
local FISHING_ICON = 136245
local SIZE = 6

local fishingNames

local function SpellName(id)
    if C_Spell and C_Spell.GetSpellName then
        return C_Spell.GetSpellName(id)
    elseif GetSpellInfo then
        return (GetSpellInfo(id))
    end
end

local function OwnFishingCheck()
    local name, _, texture, _, _, _, _, spellID = UnitChannelInfo("player")
    if not name then
        return false
    end
    if not fishingNames then
        fishingNames = {}
        for _, id in ipairs({ 131474, 7620 }) do
            local n = SpellName(id)
            if n then
                fishingNames[n] = true
            end
        end
    end
    return (spellID and FISHING_IDS[spellID]) or fishingNames[name] or texture == FISHING_ICON or false
end

local function IsFishing()
    -- Prefer Single-Button Fishing's own test (id set + locale-resolved name) when it is loaded.
    if SBF and SBF.IsFishingChannel then
        local ok, fishing = pcall(SBF.IsFishingChannel)
        if ok then
            return fishing and true or false
        end
    end
    local ok, fishing = pcall(OwnFishingCheck)
    return ok and fishing or false
end

local function IsCasting()
    local ok, name = pcall(UnitCastingInfo, "player")
    return ok and name ~= nil
end

-- No parent: unaffected by UI scale and still visible when the UI is hidden (Alt+Z).
local f = CreateFrame("Frame", "FiskaSignalFrame", nil)
f:SetFrameStrata("TOOLTIP")
f:SetSize(SIZE * 2, SIZE)
f:SetPoint("TOPLEFT", WorldFrame, "TOPLEFT", 0, 0)

local status = f:CreateTexture(nil, "OVERLAY")
status:SetPoint("TOPLEFT", f, "TOPLEFT", 0, 0)
status:SetSize(SIZE, SIZE)
status:SetColorTexture(0, 0, 1, 1)

local casting = f:CreateTexture(nil, "OVERLAY")
casting:SetPoint("TOPLEFT", f, "TOPLEFT", SIZE, 0)
casting:SetSize(SIZE, SIZE)
casting:SetColorTexture(0, 0, 1, 1)
f:Show()

local lootUntil = 0
f:RegisterEvent("LOOT_OPENED")
pcall(f.RegisterEvent, f, "LOOT_READY") -- not in every client
f:SetScript("OnEvent", function()
    lootUntil = GetTime() + 1.5
end)

local acc = 0
f:SetScript("OnUpdate", function(_, elapsed)
    acc = acc + elapsed
    if acc < 0.03 then
        return
    end
    acc = 0
    status:SetColorTexture(IsFishing() and 1 or 0, GetTime() < lootUntil and 1 or 0, 1, 1)
    casting:SetColorTexture(IsCasting() and 1 or 0, 0, 1, 1)
end)
