-- FiskaSignal: a 4x4 status square in the top-left corner of the screen,
-- read by the external fiska.py script.
--   blue  = always on (marker: the addon is loaded)
--   red   = the player is channeling Fishing (our bobber is in the water)
--   green = a loot window opened during the last 1.5 s (catch confirmed)
--
-- It reads nothing the default UI does not already show and changes nothing.

local FISHING_IDS = {
    [7620] = true, [7731] = true, [7732] = true, [18248] = true, [33095] = true,
    [51294] = true, [88868] = true, [110410] = true, [131474] = true,
    [131476] = true, [131490] = true,
}
local FISHING_ICON = 136245

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

-- No parent: unaffected by UI scale and still visible when the UI is hidden (Alt+Z).
local f = CreateFrame("Frame", "FiskaSignalFrame", nil)
f:SetFrameStrata("TOOLTIP")
f:SetSize(4, 4)
f:SetPoint("TOPLEFT", WorldFrame, "TOPLEFT", 0, 0)

local tex = f:CreateTexture(nil, "OVERLAY")
tex:SetAllPoints()
tex:SetColorTexture(0, 0, 1, 1)
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
    tex:SetColorTexture(IsFishing() and 1 or 0, GetTime() < lootUntil and 1 or 0, 1, 1)
end)
