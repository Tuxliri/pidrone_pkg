#!/usr/bin/python
import tf
import sys
import yaml
import rospy
import rospkg
import signal
import numpy as np
import command_values as cmds
from sensor_msgs.msg import Imu
from h2rMultiWii import MultiWii
from serial import SerialException
from std_msgs.msg import Header, Empty
from geometry_msgs.msg import Quaternion
from pidrone_pkg.msg import Battery, Mode, RC
import os


class FlightController(object):
    """A class that sends the current [r,p,y,t] commands to the flight
    controller board and then reads and publishes all of the data received
    from the flight controller.

    Publishers:
    /pidrone/imu
    /pidrone/battery
    /pidrone/mode

    Subscribers:
    /pidrone/commanded/mode
    /pidrone/fly_commands
    """

    def __init__(self):
        # Connect to the flight controller board
        self.board = self.getBoard()
        # stores the current and previous modes
        self.curr_mode = 'DISARMED'         #initialize as disarmed
        self.prev_mode = 'DISARMED'         #initialize as disarmed
        # store the command to send to the flight controller
        self.command = cmds.disarm_cmd      #initialize as disarmed
        # store the mode publisher
        self.modepub = None
        # store the time for angular velocity calculations
        self.time = rospy.Time.now()

        # Initialize the Imu Message
        ############################
        header = Header()
        header.frame_id = 'Body'
        header.stamp = rospy.Time.now()

        self.imu_message = Imu()
        self.imu_message.header = header

        # Initialize the Battery Message
        ################################
        self.battery_message = Battery()
        self.battery_message.vbat = None
        self.battery_message.amperage = None

        # Accelerometer parameters
        ##########################
        rospack = rospkg.RosPack()
        path = rospack.get_path('pidrone_pkg')
        with open("%s/params/multiwii.yaml" % path) as f:
            means = yaml.load(f)
        self.accRawToMss = 9.8 / means["az"]
        self.accZeroX = means["ax"] * self.accRawToMss
        self.accZeroY = means["ay"] * self.accRawToMss
        self.accZeroZ = means["az"] * self.accRawToMss


    # ROS subscriber callback methods:
    ##################################
    def commanded_mode_callback(self, msg):
        """ Set the current mode to the commanded mode """
        self.prev_mode = self.curr_mode
        self.curr_mode = msg.mode
        self.update_command()

    def fly_commands_callback(self, msg):
        """ Store and send the flight commands if the current mode is FLYING """
        if self.curr_mode == 'FLYING':
            r = msg.roll
            p = msg.pitch
            y = msg.yaw
            t = msg.throttle
            self.command = [r,p,y,t]

    # Update methods:
    #################
    def update_imu_message(self):
        """
        Compute the ROS IMU message by reading data from the board.
        """
        time = rospy.Time.now()

        # extract roll, pitch, heading
        self.board.getData(MultiWii.ATTITUDE)
        # extract lin_acc_x, lin_acc_y, lin_acc_z
        self.board.getData(MultiWii.RAW_IMU)

        # calculate values to update imu_message:
        roll = np.deg2rad(self.board.attitude['angx'])
        pitch = -np.deg2rad(self.board.attitude['angy'])
        heading = np.deg2rad(self.board.attitude['heading'])
        # Note that at pitch angles near 90 degrees, the roll angle reading can
        # fluctuate a lot
        # transform heading (similar to yaw) to standard math conventions, which
        # means angles are in radians and positive rotation is CCW
        heading = (-heading) % (2 * np.pi)
        # When first powered up, heading should read near 0
        # get the previous roll, pitch, heading values
        previous_quaternion = self.imu_message.orientation
        quaternion_array = [previous_quaternion.x, previous_quaternion.y, previous_quaternion.z, previous_quaternion.w]
        previous_roll, previous_pitch, previous_heading = tf.transformations.euler_from_quaternion(quaternion_array)
        # calculate the angular velocities of roll, pitch, and yaw in rad/s
        dt = time.to_sec() - self.time.to_sec()
        dr = roll - previous_roll
        dp = pitch - previous_pitch
        dh = heading - previous_heading
        angvx = self.near_zero(dr / dt)
        angvy = self.near_zero(dp / dt)
        angvz = self.near_zero(dh / dt)
        self.time = time

        # transform euler angles into quaternion
        quaternion = tf.transformations.quaternion_from_euler(roll, pitch, heading)
        # calculate the linear accelerations
        lin_acc_x = self.board.rawIMU['ax'] * self.accRawToMss - self.accZeroX
        lin_acc_y = self.board.rawIMU['ay'] * self.accRawToMss - self.accZeroY
        lin_acc_z = self.board.rawIMU['az'] * self.accRawToMss - self.accZeroZ

        # Rotate the IMU frame to align with our convention for the drone's body
        # frame. IMU: x is forward, y is left, z is up. We want: x is right,
        # y is forward, z is up.
        lin_acc_x_drone_body = -lin_acc_y
        lin_acc_y_drone_body = lin_acc_x
        lin_acc_z_drone_body = lin_acc_z

        # Account for gravity's affect on linear acceleration values when roll
        # and pitch are nonzero. When the drone is pitched at 90 degrees, for
        # example, the z acceleration reads out as -9.8 m/s^2. This makes sense,
        # as the IMU, when powered up / when the calibration script is called,
        # zeros the body-frame z-axis acceleration to 0, but when it's pitched
        # 90 degrees, the body-frame z-axis is perpendicular to the force of
        # gravity, so, as if the drone were in free-fall (which was roughly
        # confirmed experimentally), the IMU reads -9.8 m/s^2 along the z-axis.
        g = 9.8
        lin_acc_x_drone_body = lin_acc_x_drone_body + g*np.sin(roll)*np.cos(pitch)
        lin_acc_y_drone_body = lin_acc_y_drone_body + g*np.cos(roll)*(-np.sin(pitch))
        lin_acc_z_drone_body = lin_acc_z_drone_body + g*(1 - np.cos(roll)*np.cos(pitch))

        # Update the imu_message:
        # header stamp
        self.imu_message.header.stamp = time
        # orientation
        self.imu_message.orientation.x = quaternion[0]
        self.imu_message.orientation.y = quaternion[1]
        self.imu_message.orientation.z = quaternion[2]
        self.imu_message.orientation.w = quaternion[3]
        # angular velocities
        self.imu_message.angular_velocity.x = angvx
        self.imu_message.angular_velocity.y = angvy
        self.imu_message.angular_velocity.z = angvz
        # linear accelerations
        self.imu_message.linear_acceleration.x = lin_acc_x_drone_body
        self.imu_message.linear_acceleration.y = lin_acc_y_drone_body
        self.imu_message.linear_acceleration.z = lin_acc_z_drone_body

    def update_battery_message(self):
        """
        Compute the ROS battery message by reading data from the board.
        """
        # extract vbat, amperage
        self.board.getData(MultiWii.ANALOG)
        # Update Battery message:
        self.battery_message.vbat = self.board.analog['vbat'] * 0.10
        self.battery_message.amperage = self.board.analog['amperage']

    def update_command(self):
        ''' Set command values if the mode is ARMED or DISARMED '''
        if self.curr_mode == 'DISARMED':
            self.command = cmds.disarm_cmd
        elif self.curr_mode == 'ARMED':
            if self.prev_mode == 'DISARMED':
                self.command = cmds.arm_cmd
            elif self.prev_mode == 'ARMED':
                self.command = cmds.idle_cmd

    # Helper Methods:
    #################
    def getBoard(self):
        """ Connect to the flight controller board """
        # (if the flight cotroll usb is unplugged and plugged back in,
        #  it becomes .../USB1)
        try:
            board = MultiWii('/dev/ttyUSB0')
        except SerialException:
            try:
                board = MultiWii('/dev/ttyUSB1')
            except SerialException:
                print '\nCannot connect to the flight controller board.'
                print 'The USB is unplugged. Please check connection.'
                sys.exit()
        return board

    def send_cmd(self):
        """ Send commands to the flight controller board """
        self.board.sendCMD(8, MultiWii.SET_RAW_RC, self.command)
        self.board.receiveDataPacket()
        print 'command sent:', self.command

    def near_zero(self, n):
        """ Set a number to zero if it is below a threshold value """
        return 0 if abs(n) < 0.0001 else n

    def ctrl_c_handler(self, signal, frame):
        """ Disarm the drone and quits the flight controller node """
        print "\nCaught ctrl-c! About to Disarm!"
        self.board.sendCMD(8, MultiWii.SET_RAW_RC, cmds.disarm_cmd)
        self.board.receiveDataPacket()
        rospy.sleep(1)
        self.modepub.publish('DISARMED')
        print "Successfully Disarmed"
        sys.exit()


def main():
    # ROS Setup
    ###########
    node_name = os.path.splitext(os.path.basename(__file__))[0]
    rospy.init_node(node_name)

    # create the FlightController object
    fc = FlightController()

    # Publisher
    ###########
    imupub = rospy.Publisher('/pidrone/imu', Imu, queue_size=1, tcp_nodelay=False)
    batpub = rospy.Publisher('/pidrone/battery', Battery, queue_size=1, tcp_nodelay=False)
    fc.modepub = rospy.Publisher('/pidrone/mode', Mode, queue_size=1, tcp_nodelay=False)
    fc.heartbeat_pub = rospy.Publisher('/pidrone/heartbeat/flight_controller', Empty, queue_size=1, tcp_nodelay=False)
    print 'Publishing:'
    print '/pidrone/imu'
    print '/pidrone/mode'
    print '/pidrone/battery'
    print '/heartbeat/flight_controller'

    # Subscriber
    ############
    rospy.Subscriber('/pidrone/commanded/mode', Mode, fc.commanded_mode_callback)
    rospy.Subscriber('/pidrone/fly_commands', RC, fc.fly_commands_callback)


    signal.signal(signal.SIGINT, fc.ctrl_c_handler)
    # set the loop rate (Hz)
    r = rospy.Rate(60)
    try:
        while not rospy.is_shutdown():
            fc.heartbeat_pub.publish(Empty())
            # update and publish flight controller readings
            fc.update_battery_message()
            fc.update_imu_message()
            imupub.publish(fc.imu_message)
            batpub.publish(fc.battery_message)

            # update and send the flight commands to the board
            fc.update_command()
            fc.send_cmd()

            # publish the current mode of the drone
            fc.modepub.publish(fc.curr_mode)

            # sleep for the remainder of the loop time
            r.sleep()

        print 'Shutdown received'
        fc.board.sendCMD(8, MultiWii.SET_RAW_RC, cmds.disarm_cmd)
        fc.board.receiveDataPacket()
    except SerialException:
        print '\nCannot connect to the flight controller board.'
        print 'The USB is unplugged. Please check connection.'


if __name__ == '__main__':
    main()
