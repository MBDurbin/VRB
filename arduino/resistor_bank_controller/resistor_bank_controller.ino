// --- Function Prototypes ---
void shedAllLoad();
void feedWatchdog();
bool isValidCommand(String str);
bool isAllZeros(String str);
void updateOtherRelays(String binaryStr);

// String to hold the current state
String currentState = "";
String MainRelay = "Open";
bool noSignalAnnounced = false;

// ================= HARDWARE CONFIGURATION =================
const int RELAY_MAIN_PIN = 4;       // Main relay d4
const int BANK_1_RELAY   = 5;        // Bank 1, 0.25 ohm: the LAST character
const int otherRelayPins[] = {12, 11, 10, 9, 8, 7, 6}; // Banks 8 (32 ohm) down to 2 (0.5 ohm)
const int numOtherRelays = sizeof(otherRelayPins) / sizeof(otherRelayPins[0]);

// One character per ladder relay: otherRelayPins in order, then BANK_1_RELAY.
// The host always sends exactly this many (send_binary_command in Python).
//
// The word is the number of 0.25 ohm steps in binary, most significant bank
// first: 32 ohm is 10000000, 0.25 ohm is 00000001. The host's copy of this
// mapping is COMMAND_BANKS and BANK_RELAY_PIN in control_logic.py, and
// tests/test_firmware_bank_map.py compiles this sketch to check the two agree.
// Change the pins here and the test fails until the Python is changed to match.
const int COMMAND_LENGTH = numOtherRelays + 1;

const int RELAY_OPEN  = LOW;  // LED OFF
const int RELAY_CLOSE = HIGH;  // LED ON

// ================= CLOCK VARIABLES =================
unsigned long lastConnectionTime = 0;  
const long TIMEOUT_LIMIT = 2000;        

// ================= SETUP =================
void setup() {
  pinMode(RELAY_MAIN_PIN, OUTPUT);
  digitalWrite(RELAY_MAIN_PIN, RELAY_OPEN);
  MainRelay = "Open";

  pinMode(BANK_1_RELAY, OUTPUT);
  digitalWrite(BANK_1_RELAY, RELAY_OPEN); //turn on the 0.25 ohm resistor

  for (int i = 0; i < numOtherRelays; i++) {
    pinMode(otherRelayPins[i], OUTPUT);
    digitalWrite(otherRelayPins[i], RELAY_OPEN); //turn on all other resistors
  }

  Serial.begin(9600);
  Serial.println("Arduino Ready. Waiting for Binary Signal...");
}

// ================= MAIN LOOP =================
void loop() {

  // 1. WATCHDOG CHECK
  if (millis() - lastConnectionTime > TIMEOUT_LIMIT) {
      shedAllLoad();
  }

// 2. CHECK FOR INCOMING DATA
  if (Serial.available() > 0) {

    String incomingData = Serial.readStringUntil('\n');
    incomingData.trim();

    // 3. IDENTIFY THE MESSAGE BEFORE ACTING ON IT
    // Only the four messages the host actually sends are acted on. Anything
    // else -- serial noise, a line cut short by the read timeout, a binary word
    // of the wrong length -- is rejected without touching a relay or feeding
    // the watchdog.
    //
    // The main relay used to close as soon as a message got past the
    // handshakes, BEFORE it was checked. A malformed line closed it and left it
    // closed until the watchdog fired, and even KILL closed it an instant
    // before opening it again.
    //
    // Only "alive" and a valid resistance command feed the watchdog: they are
    // the host's control traffic. The handshake and KILL are answered but do not
    // count as proof the host is still in control.
    if (incomingData == "?WHOAMI") {
      Serial.println("RESISTOR_CTRL");
      return;
    }

    if (incomingData == "alive") {
      feedWatchdog();
      return;
    }

    // Matches Python's uppercase "KILL"; lowercase kept for bench_relay_check.
    if (incomingData == "KILL" || incomingData == "kill") {
      shedAllLoad();
      return;
    }

    if (!isValidCommand(incomingData)) {
      Serial.print("Error: Rejected. A command is exactly ");
      Serial.print(COMMAND_LENGTH);
      Serial.println(" binary digits.");
      return;
    }

    feedWatchdog();

    // SAFETY INTERLOCK
    if (isAllZeros(incomingData)) {
       Serial.println("SAFETY ACTION: All-Zero detected. Adjusting...");
       incomingData.setCharAt(incomingData.length() - 1, '1');
    }

    // Only now, holding a complete and valid resistance command, connect the
    // bank. When the main relay is open every ladder relay is open too
    // (shedAllLoad and setup both guarantee it), so it closes onto maximum
    // resistance and the ladder steps down to the target below.
    if (MainRelay == "Open"){
      digitalWrite(RELAY_MAIN_PIN, RELAY_CLOSE);
      MainRelay = "Close";
    }

    // State change check
    if (incomingData == currentState) {
      return;
    }

    Serial.print("New State Received: ");
    Serial.println(incomingData);

    // OPEN 0.25 ohm relay (Safety Step)
    digitalWrite(BANK_1_RELAY, RELAY_OPEN);
    Serial.println("Action: 0.25 relay opened");
    
    // Fixed: 50ms provides enough time for mechanical relay clearance and debouncing
    // without stalling the 1Hz Python physics loop.
    delay(50); 

    // Switch other relays
    updateOtherRelays(incomingData);
    Serial.println("Action: All Other Relays Changed");
    
    delay(50); 

    // Check last bit logic
    char lastChar = incomingData.charAt(incomingData.length() - 1);

    if (lastChar == '1') {
      digitalWrite(BANK_1_RELAY, RELAY_OPEN);
      Serial.println("Action: 0.25 ohm Relay Kept OPEN.");
    }
    else {
      digitalWrite(BANK_1_RELAY, RELAY_CLOSE);
      Serial.println("Action: 0.25 ohm Relay Kept CLOSED.");
    }

    currentState = incomingData;
  }
}

// ================= HELPER FUNCTIONS =================

// Disconnect the bank and return it to its safest state.
//
// Called on the 2 s serial timeout and on an explicit KILL. This is the rig's
// independent hardware safety layer -- it runs whether or not the host is
// healthy, and it is what protects the bank while the Python side is stalled
// in a COM-port scan or a hung DAQ.
//
// Two distinct things happen here, and the second is the one that matters:
//
//   1. Every bank relay is driven RELAY_OPEN, which puts ALL resistors into
//      circuit -- maximum resistance, 63.75 ohm, so minimum current. Note this
//      is the opposite sense to what "open" suggests at first glance: an open
//      relay does not remove a resistor, it stops bypassing it.
//   2. RELAY_MAIN_PIN is driven open, disconnecting the bank from the battery
//      entirely. THIS is what actually sheds the load.
//
// This function was previously called turnONAllRESISTORS(), which described
// only step 1 and said nothing about the main contactor -- the safety-critical
// half. Renamed so the name states the outcome rather than a side effect.
void shedAllLoad() {
  digitalWrite(BANK_1_RELAY, RELAY_OPEN);
  for (int i = 0; i < numOtherRelays; i++) {
    digitalWrite(otherRelayPins[i], RELAY_OPEN);
  }
  digitalWrite(RELAY_MAIN_PIN, RELAY_OPEN);

  currentState = "";
  MainRelay = "Open";

  if (!noSignalAnnounced) {
    Serial.println("No Signal: System Reset / All Off");
    noSignalAnnounced = true;
  }
}

// Called only for an "alive" heartbeat or a valid resistance command. Any other
// line -- noise, an unrelated program writing to the port, a malformed command --
// leaves the timer running, so the bank still sheds 2 s after real control
// traffic stops, however much else is arriving.
void feedWatchdog() {
  lastConnectionTime = millis();
  noSignalAnnounced = false;
}

// A resistance command is exactly one 0/1 character per ladder relay. A shorter
// word would update only some relays and leave the rest at their old setting; a
// longer one carries bits no relay listens to. Either way the bank would not be
// at the resistance the host asked for.
bool isValidCommand(String str) {
  if ((int)str.length() != COMMAND_LENGTH) return false;
  for (unsigned int i = 0; i < str.length(); i++) {
    if (str.charAt(i) != '0' && str.charAt(i) != '1') {
      return false;
    }
  }
  return true;
}

bool isAllZeros(String str) {
  for (unsigned int i = 0; i < str.length(); i++) {
    if (str.charAt(i) != '0') {
      return false;
    }
  }
  return true;
}

void updateOtherRelays(String binaryStr) {
  int limit = min((int)binaryStr.length(), numOtherRelays);
  for (int i = 0; i < limit; i++) {
    char bit = binaryStr.charAt(i);
    digitalWrite(otherRelayPins[i], (bit == '1') ? RELAY_OPEN : RELAY_CLOSE);
  }
}