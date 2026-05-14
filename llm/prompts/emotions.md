You analyze Hindi/Hinglish/English text for vocal emotion.

Input format: a JSON array of sentences.

Task: for EACH sentence, decide what emotion is being voiced in that
sentence. If the sentence is purely descriptive (scenery, weather,
narration with no vocal emotion) output null.

Available tags (pick ONE per emotional sentence, or null):
  "[crying]" "[laughs]" "[chuckles]" "[sighs]" "[whispers]"
  "[excited]" "[happy]" "[sad]" "[angry]" "[shouting]"
  "[gasps]" "[breathless]" "[hesitant]" "[serious]"

Decision guide (semantic — use your judgement, not just keywords):
- crying / sobbing / writhing in pain / heart-shaking sounds → [crying]
- laughter / joy in voice → [laughs]
- whispering / quiet secrets / muttering → [whispers]
- shouting / yelling / angry voice → [shouting] or [angry]
- grim / hopeless / sorrowful dialogue → [sad]
- gasping / surprised intake of breath → [gasps]
- tired / out of breath speech → [breathless]
- hesitant / stammering / unsure speech → [hesitant]
- formal / serious dialogue → [serious]
- excited / energetic happy speech → [excited]
- pure scene description, neutral narration → null

Output: ONLY a JSON object with an "emotions" array of length equal to
input length. Each entry is either a tag string (with brackets) or null.
NO commentary, NO markdown, NO explanation. Just the JSON.

EXAMPLE 1
Input:  ["रह-रहकर उसके मुँह से ऐसी दिल हिला देने वाली आवाज़ निकलती थी।", "जाड़ों की रात थी, सारा गाँव अंधकार में लय हो गया था।", "घीसू ने कहा—'मालूम होता है, बचेगी नहीं।'"]
Output: {"emotions": ["[crying]", null, "[sad]"]}

EXAMPLE 2
Input:  ["वह तड़पना और हाथ-पाँव पटकना नहीं देखा जाता।", "दूर कहीं घंटी बजती रही।", "वह हँसते हुए बोला—'अरे यार!'"]
Output: {"emotions": ["[crying]", null, "[laughs]"]}

EXAMPLE 3
Input:  ["आज मौसम बहुत अच्छा है।", "उसने धीरे से कहा—'मुझे माफ कर दो।'"]
Output: {"emotions": [null, "[whispers]"]}

Now classify the input below.
