#include <Arduino.h>
#include <RotaryEncoder.h>
#include <Servo.h>
#include <string.h>

// Pin definitions
#define ENCODER_PIN1 2
#define ENCODER_PIN2 3
#define SERVO_PIN 9

// Servo angles
#define INITIAL_ANGLE 125
#define TARGET_ANGLE 175

// Thresholds
#define INACTIVITY_THRESHOLD 20000UL
#define POSITION_THRESHOLD 50

RotaryEncoder encoder(ENCODER_PIN1, ENCODER_PIN2);
Servo servo;

unsigned long lastMovementTime = 0;
int lastEncoderPosition = 0;
bool movingToTarget = false;
bool automatic = true;

char lockstate[20] = "automatic";
char prevLockstate[20] = "automatic"; 

int locomotionBinary = 0;

// --- SERIAL OUTPUT VARIABLES ---
unsigned long lastSerialTime = 0;
const unsigned long SERIAL_INTERVAL = 50; // 50 ms

// --- TIME-BASED SERVO VARIABLES ---
int startAngle = INITIAL_ANGLE;
int targetAngle = INITIAL_ANGLE;
unsigned long moveStartTime = 0;
float currentAngleF = INITIAL_ANGLE; // float for smooth movement

// Speeds to exactly match original blocking delays
const float FORWARD_SPEED_DPS  = 5.0;   // 200 ms per degree
const float BACKWARD_SPEED_DPS = 20.0;  // 50 ms per degree

void setup() {
  Serial.begin(9600);

  servo.attach(SERVO_PIN);
  servo.write(INITIAL_ANGLE);

  lastMovementTime = millis();
  strcpy(prevLockstate, lockstate);  
}

void loop() {
  unsigned long now = millis();

  // --- SERIAL INPUT ---
  if (Serial.available() > 0) {
    char input = Serial.read();

    if (input == 'a') {
      automatic = true;
      strcpy(lockstate, "automatic");
    }
    else if (input == 'b') {
      automatic = false;
      strcpy(lockstate, "locked");
    }
    else if (input == 'c') {
      automatic = false;
      strcpy(lockstate, "unlocked");
    }
  }

  // --- ENCODER ---
  encoder.tick();
  int currentPosition = encoder.getPosition();

  // --- MOVEMENT DETECTION ---
  if (abs(currentPosition - lastEncoderPosition) > POSITION_THRESHOLD) {

      lastMovementTime = now;
      locomotionBinary = 1;

      if (automatic) {
          startAngle = currentAngleF;
          targetAngle = INITIAL_ANGLE;
          moveStartTime = now;
          movingToTarget = false;
      }

      lastEncoderPosition = currentPosition;
  }

  // --- INACTIVITY ---
  if (now - lastMovementTime >= INACTIVITY_THRESHOLD) {
    locomotionBinary = 0;

    if (automatic && !movingToTarget) {
      startAngle = currentAngleF;
      targetAngle = TARGET_ANGLE;
      moveStartTime = now;
      movingToTarget = true;
    }
  }

  // --- LOCK / UNLOCK ---
  if (strcasecmp(lockstate, prevLockstate) != 0) {
    startAngle = currentAngleF;

    if (strcasecmp(lockstate, "locked") == 0) {
      targetAngle = TARGET_ANGLE;
    }
    else if (strcasecmp(lockstate, "unlocked") == 0) {
      targetAngle = INITIAL_ANGLE;
    }

    moveStartTime = now;
    strcpy(prevLockstate, lockstate);   
  }

  // --- TIME-BASED NON-BLOCKING SERVO MOVE ---
  if ((int)currentAngleF != targetAngle) {
    float speed = (targetAngle > startAngle) ? BACKWARD_SPEED_DPS : FORWARD_SPEED_DPS;
    float elapsedSec = (now - moveStartTime) / 1000.0;
    float delta = elapsedSec * speed;

    if (targetAngle > startAngle)
      currentAngleF = startAngle + delta;
    else
      currentAngleF = startAngle - delta;

    // Constrain to target
    if ((targetAngle > startAngle && currentAngleF > targetAngle) ||
        (targetAngle < startAngle && currentAngleF < targetAngle)) {
      currentAngleF = targetAngle;
    }

    servo.write((int)currentAngleF);
  }

  // --- SERIAL OUTPUT (EVERY 50 ms) ---
  if (now - lastSerialTime >= SERIAL_INTERVAL) {
    Serial.print(locomotionBinary);
    Serial.print(";");
    Serial.print(currentPosition);
    Serial.print(";");
    Serial.println(lockstate);

    lastSerialTime = now;
  }
}
